/*
 * FreeRTOS Kernel V11.3.1
 * Copyright (C) 2020 Cambridge Consultants Ltd.
 * Copyright (c) 2026 The RadioFoundry Authors (the deterministic host changes)
 *
 * SPDX-License-Identifier: MIT
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy of
 * this software and associated documentation files (the "Software"), to deal in
 * the Software without restriction, including without limitation the rights to
 * use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
 * the Software, and to permit persons to whom the Software is furnished to do so,
 * subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
 * FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 * COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
 * IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * https://www.FreeRTOS.org
 * https://github.com/FreeRTOS
 *
 */

/*-----------------------------------------------------------
 * A deterministic host port, forked from portable/ThirdParty/GCC/Posix/port.c.
 *
 * Kept from the POSIX port: each task is a pthread, and exactly one of them runs; the others wait on their own
 * event. A task switch is vTaskSwitchContext() then a hand-off: wake the new thread, park the old one.
 *
 * Changed:
 * - No tick thread and no signals at all. Nothing interrupts C code between two statements.
 * - "Interrupts" are numbered sources with a pending bit each (platform/host/vport.h). Raising one runs its
 *   handler at once on the running task's thread if interrupts are on; otherwise it stays pending and runs when they
 *   come back on (the end of the critical section). Lower number = higher priority.
 * - A task switch request stays pending while interrupts are masked or a handler runs; the switch happens after
 *   interrupts are enabled and all pending handlers have run, as PendSV does on the radio's CPU.
 * - The tick is interrupt 0, raised by the virtual clock (platform/host/vclock.c), never by the wall clock.
 * - A new thread is fenced: its creator waits until it has parked, so no thread runs unasked.
 *----------------------------------------------------------*/
#define _GNU_SOURCE
#include <pthread.h>
#include <semaphore.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"

/* A latched wake-up: a signal before the wait is not lost */
typedef struct
{
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    bool triggered;
} Event_t;

typedef struct THREAD
{
    pthread_t pthread;
    TaskFunction_t pxCode;
    void * pvParams;
    BaseType_t xDying;
    Event_t * ev;
} Thread_t;

static inline Thread_t * prvGetThreadFromTask( TaskHandle_t xTask )
{
    StackType_t * pxTopOfStack = *( StackType_t ** ) xTask;

    return ( Thread_t * ) ( pxTopOfStack + 1 );
}

static UBaseType_t uxCriticalNesting;       /* the running task's; saved across a switch */
static bool xIrqOff = true;                 /* interrupts masked (critical section, before the scheduler) */
static bool xInIsr;                         /* a simulated handler runs now */
static bool xYieldPending;                  /* a task or handler asked for a switch once interrupts are on */
static bool xSchedulerStarted;
static uint32_t ulPending;                  /* pending sources, bit n = interrupt n */
static vhost_isr_t pxHandlers[ VHOST_IRQ_COUNT ];
static const char * pcNames[ VHOST_IRQ_COUNT ];
static sem_t xParked;                       /* the creation fence */
static pthread_key_t xThreadKey;
static pthread_once_t xKeyOnce = PTHREAD_ONCE_INIT;
static Event_t xMainEvent;                  /* the main thread waits here for ever */
static uint64_t ulCriticals;                /* outermost critical sections entered by tasks */
static uint64_t ulDeferred;                 /* interrupts raised while masked, run later */
static uint64_t ulSuspends;                 /* vTaskSuspendAll calls from outside the kernel */

/* The three counts above are written by the running task and read by vport_stats(), which vhost_finish() may call
 * from a thread that is not a task (the stall watch): relaxed atomics, so that read is not a data race */
#define prvCount( x )    ( ( void ) __atomic_fetch_add( &( x ), 1U, __ATOMIC_RELAXED ) )
#define prvRead( x )     __atomic_load_n( &( x ), __ATOMIC_RELAXED )

static void prvEventInit( Event_t * ev )
{
    pthread_mutex_init( &ev->mutex, NULL );
    pthread_cond_init( &ev->cond, NULL );
    ev->triggered = false;
}

static void prvEventWait( Event_t * ev )
{
    pthread_mutex_lock( &ev->mutex );

    while( !ev->triggered )
    {
        pthread_cond_wait( &ev->cond, &ev->mutex );
    }

    ev->triggered = false;
    pthread_mutex_unlock( &ev->mutex );
}

static void prvEventSignal( Event_t * ev )
{
    pthread_mutex_lock( &ev->mutex );
    ev->triggered = true;
    pthread_cond_signal( &ev->cond );
    pthread_mutex_unlock( &ev->mutex );
}

static void prvKeyInit( void )
{
    pthread_key_create( &xThreadKey, NULL );
    sem_init( &xParked, 0, 0 );
}

static BaseType_t prvIsFreeRTOSThread( void )
{
    ( void ) pthread_once( &xKeyOnce, prvKeyInit );
    return pthread_getspecific( xThreadKey ) != NULL ? pdTRUE : pdFALSE;
}

static void prvSuspendSelf( Thread_t * thread )
{
    prvEventWait( thread->ev );

    if( thread->xDying == pdTRUE )
    {
        pthread_exit( NULL );       /* deleted while parked (vPortCancelThread) */
    }
}

static void prvResumeThread( Thread_t * thread )
{
    if( !pthread_equal( pthread_self(), thread->pthread ) )
    {
        prvEventSignal( thread->ev );
    }
}

static void prvSwitchThread( Thread_t * pxThreadToResume,
                             Thread_t * pxThreadToSuspend )
{
    if( pxThreadToSuspend != pxThreadToResume )
    {
        UBaseType_t uxSaved = uxCriticalNesting;
        bool xSavedOff = xIrqOff;
        /* Read before the successor runs: its clean-up may write the field */
        BaseType_t xDying = pxThreadToSuspend->xDying;

        prvResumeThread( pxThreadToResume );

        if( xDying == pdTRUE )
        {
            pthread_exit( NULL );
        }

        prvSuspendSelf( pxThreadToSuspend );
        uxCriticalNesting = uxSaved;
        xIrqOff = xSavedOff;
    }
}

/* Select the next task and hand the CPU to it (interrupts masked meanwhile, as in the POSIX port) */
static void prvSwitchNow( void )
{
    Thread_t * pxOld;
    Thread_t * pxNew;
    bool xWasOff = xIrqOff;

    uxCriticalNesting++;
    xIrqOff = true;
    pxOld = prvGetThreadFromTask( xTaskGetCurrentTaskHandle() );
    vTaskSwitchContext();
    pxNew = prvGetThreadFromTask( xTaskGetCurrentTaskHandle() );
    prvSwitchThread( pxNew, pxOld );
    uxCriticalNesting--;
    xIrqOff = xWasOff;
}

/* Run the pending handlers, highest priority first, while interrupts are on and we are on a task's thread. A switch
 * waits until every pending handler has run: on the radio's CPU the switch is the PendSV exception,
 * at the lowest priority (third_party/FreeRTOS-Kernel/portable/GCC/ARM_CM4F/port.c:66), so pending interrupts
 * tail-chain before it. The context switch itself stays behind prvSwitchNow() */
static void prvDeliverPending( void )
{
    for( ; ; )
    {
        while( ( ulPending != 0U ) && !xIrqOff && !xInIsr && xSchedulerStarted && prvIsFreeRTOSThread() )
        {
            unsigned irq = ( unsigned ) __builtin_ctz( ulPending );

            ulPending &= ~( 1UL << irq );
            xInIsr = true;
            xIrqOff = true;
            vstall_progress();
            vtrace_isr( pcNames[ irq ] != NULL ? pcNames[ irq ] : "?" );

            if( pxHandlers[ irq ] != NULL )
            {
                pxHandlers[ irq ]();
            }

            xIrqOff = false;
            xInIsr = false;
        }

        if( !xYieldPending || xIrqOff || xInIsr || !xSchedulerStarted || !prvIsFreeRTOSThread() )
        {
            break;
        }

        xYieldPending = false;
        prvSwitchNow();
    }
}

void vhost_irq_register( unsigned irq,
                         const char * name,
                         vhost_isr_t isr )
{
    configASSERT( irq < VHOST_IRQ_COUNT );
    pxHandlers[ irq ] = isr;
    pcNames[ irq ] = name;
}

void vhost_irq_raise( unsigned irq )
{
    configASSERT( irq < VHOST_IRQ_COUNT );
    if( xIrqOff || xInIsr )
    {
        prvCount( ulDeferred );
    }

    ulPending |= 1UL << irq;

#if defined( HOST_PORT_TEST_BREAK ) && ( HOST_PORT_TEST_BREAK == 1 )
    /* A deliberately wrong build, only for the port's unit test (boards/vhost/tests/hostPortTest.c): it delivers at
     * raise time even inside a critical section, so the test can show it catches that */
    if( xIrqOff && !xInIsr )
    {
        xIrqOff = false;
        prvDeliverPending();
        xIrqOff = true;
        return;
    }
#endif

    prvDeliverPending();
}

bool vhost_in_isr( void )
{
    return xInIsr;
}

static void * prvWaitForStart( void * pvParams )
{
    Thread_t * pxThread = pvParams;

    ( void ) pthread_once( &xKeyOnce, prvKeyInit );
    pthread_setspecific( xThreadKey, pxThread );
    sem_post( &xParked );           /* the creator may go on: this thread does nothing until it is resumed */
    prvSuspendSelf( pxThread );

    uxCriticalNesting = 0;
    xIrqOff = false;
    pthread_setname_np( pthread_self(), pcTaskGetName( xTaskGetCurrentTaskHandle() ) );
    prvDeliverPending();
    pxThread->pxCode( pxThread->pvParams );
    configASSERT( pdFALSE );        /* a task returned */
    return NULL;
}

StackType_t * pxPortInitialiseStack( StackType_t * pxTopOfStack,
                                     StackType_t * pxEndOfStack,
                                     TaskFunction_t pxCode,
                                     void * pvParameters )
{
    Thread_t * thread;
    int iRet;

    ( void ) pthread_once( &xKeyOnce, prvKeyInit );
    thread = ( Thread_t * ) ( pxTopOfStack + 1 ) - 1;
    pxTopOfStack = ( StackType_t * ) thread - 1;
    configASSERT( ( size_t ) ( pxTopOfStack + 1 - pxEndOfStack ) * sizeof( *pxTopOfStack ) > sizeof( Thread_t ) );

    thread->pxCode = pxCode;
    thread->pvParams = pvParameters;
    thread->xDying = pdFALSE;
    thread->ev = malloc( sizeof( Event_t ) );
    configASSERT( thread->ev != NULL );
    prvEventInit( thread->ev );

    iRet = pthread_create( &thread->pthread, NULL, prvWaitForStart, thread );

    if( iRet != 0 )
    {
        fprintf( stderr, "pthread_create: %s\n", strerror( iRet ) );
        abort();
    }

    while( sem_wait( &xParked ) != 0 )
    {
    }

    return pxTopOfStack;
}

BaseType_t xPortStartScheduler( void )
{
    prvEventInit( &xMainEvent );
    xSchedulerStarted = true;
    vclock_tick_start();
    vstall_start();
    prvResumeThread( prvGetThreadFromTask( xTaskGetCurrentTaskHandle() ) );

    for( ; ; )
    {
        prvEventWait( &xMainEvent );    /* never signaled: the run ends with vhost_finish() on a task's thread */
    }

    return 0;
}

void vPortEndScheduler( void )
{
    vhost_finish( "vTaskEndScheduler", 0 );
}

void vPortEnterCritical( void )
{
    if( uxCriticalNesting == 0 )
    {
        vPortDisableInterrupts();

        if( xSchedulerStarted )
        {
            prvCount( ulCriticals );
        }
    }

    uxCriticalNesting++;
}

void vPortExitCritical( void )
{
    configASSERT( uxCriticalNesting > 0 );
    uxCriticalNesting--;

    if( uxCriticalNesting == 0 )
    {
        vPortEnableInterrupts();
    }
}

void vPortYield( void )
{
    if( !xInIsr )
    {
        configASSERT( prvIsFreeRTOSThread() == pdTRUE );
    }

    xYieldPending = true;
    prvDeliverPending();
}

void vPortDisableInterrupts( void )
{
    xIrqOff = true;
}

void vPortEnableInterrupts( void )
{
    xIrqOff = false;
    prvDeliverPending();
}

UBaseType_t xPortSetInterruptMask( void )
{
    UBaseType_t was = xIrqOff ? 1U : 0U;

    xIrqOff = true;
    return was;
}

void vPortClearInterruptMask( UBaseType_t uxMask )
{
    xIrqOff = uxMask != 0U;

    if( !xIrqOff )
    {
        prvDeliverPending();
    }
}

void vPortThreadDying( void * pxTaskToDelete,
                       volatile BaseType_t * pxPendYield )
{
    ( void ) pxPendYield;
    prvGetThreadFromTask( pxTaskToDelete )->xDying = pdTRUE;
}

void vPortCancelThread( void * pxTaskToDelete )
{
    Thread_t * pxThread = prvGetThreadFromTask( pxTaskToDelete );

    /* Either it already left in prvSwitchThread (it deleted itself), or it is parked: wake it to leave. No
     * pthread_cancel, which could leave its event's mutex locked */
    pxThread->xDying = pdTRUE;
    prvEventSignal( pxThread->ev );
    pthread_join( pxThread->pthread, NULL );
    pthread_mutex_destroy( &pxThread->ev->mutex );
    pthread_cond_destroy( &pxThread->ev->cond );
    free( pxThread->ev );
}

void vport_stats( vport_stats_t * st )
{
    st->criticals = prvRead( ulCriticals );
    st->deferred = prvRead( ulDeferred );
    st->suspends = prvRead( ulSuspends );
}

/* The firmware's scheduler locks (data locks in core/), counted: -Wl,--wrap=vTaskSuspendAll (platform.mk) reaches only
 * calls from outside the kernel's tasks.c, which is what the count is for */
void __real_vTaskSuspendAll( void );
void __wrap_vTaskSuspendAll( void );
void __wrap_vTaskSuspendAll( void )
{
    prvCount( ulSuspends );
    __real_vTaskSuspendAll();
}
