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
/* Forked from portable/ThirdParty/GCC/Posix/portmacro.h for the deterministic host port (port.c here). Changes: a 32
 * bit tick as on the radios, and portEND_SWITCHING_ISR defers the switch to the end of the simulated interrupt. */
#ifndef PORTMACRO_H
#define PORTMACRO_H

#include <limits.h>
#include <stdint.h>

#define portCHAR                 char
#define portFLOAT                float
#define portDOUBLE               double
#define portLONG                 long
#define portSHORT                short
#define portSTACK_TYPE           unsigned long
#define portBASE_TYPE            long
#define portPOINTER_SIZE_TYPE    intptr_t

typedef portSTACK_TYPE   StackType_t;
typedef long             BaseType_t;
typedef unsigned long    UBaseType_t;

typedef uint32_t         TickType_t;
#define portMAX_DELAY              ( ( TickType_t ) 0xffffffffUL )
#define portTICK_TYPE_IS_ATOMIC    1

#define portSTACK_GROWTH                   ( -1 )
#define portHAS_STACK_OVERFLOW_CHECKING    ( 1 )
#define portTICK_PERIOD_MS                 ( ( TickType_t ) 1000 / configTICK_RATE_HZ )
#define portTICK_RATE_MICROSECONDS         ( ( TickType_t ) 1000000 / configTICK_RATE_HZ )
#define portBYTE_ALIGNMENT                 8

extern void vPortYield( void );
#define portYIELD()                vPortYield()

/* Inside a simulated interrupt vPortYield only records the request; the port switches when the handler returns. */
#define portEND_SWITCHING_ISR( xSwitchRequired ) \
    do                                           \
    {                                            \
        if( ( xSwitchRequired ) != pdFALSE )     \
        {                                        \
            vPortYield();                        \
        }                                        \
    } while( 0 )
#define portYIELD_FROM_ISR( x )    portEND_SWITCHING_ISR( x )

extern void vPortDisableInterrupts( void );
extern void vPortEnableInterrupts( void );
#define portSET_INTERRUPT_MASK()      ( vPortDisableInterrupts() )
#define portCLEAR_INTERRUPT_MASK()    ( vPortEnableInterrupts() )

extern UBaseType_t xPortSetInterruptMask( void );
extern void vPortClearInterruptMask( UBaseType_t xMask );

extern void vPortEnterCritical( void );
extern void vPortExitCritical( void );
#define portSET_INTERRUPT_MASK_FROM_ISR()         xPortSetInterruptMask()
#define portCLEAR_INTERRUPT_MASK_FROM_ISR( x )    vPortClearInterruptMask( x )
#define portDISABLE_INTERRUPTS()                  portSET_INTERRUPT_MASK()
#define portENABLE_INTERRUPTS()                   portCLEAR_INTERRUPT_MASK()
#define portENTER_CRITICAL()                      vPortEnterCritical()
#define portEXIT_CRITICAL()                       vPortExitCritical()

extern void vPortThreadDying( void * pxTaskToDelete,
                              volatile BaseType_t * pxPendYield );
extern void vPortCancelThread( void * pxTaskToDelete );
#define portPRE_TASK_DELETE_HOOK( pvTaskToDelete, pxPendYield )    vPortThreadDying( ( pvTaskToDelete ), ( pxPendYield ) )
#define portCLEAN_UP_TCB( pxTCB )                                  vPortCancelThread( pxTCB )

#define portTASK_FUNCTION_PROTO( vFunction, pvParameters )         void vFunction( void * pvParameters ) __attribute__( ( noreturn ) )
#define portTASK_FUNCTION( vFunction, pvParameters )               void vFunction( void * pvParameters )

/* One thread runs at a time and every hand-off goes through a mutex: a compiler barrier is enough. */
#define portMEMORY_BARRIER()    __asm volatile ( "" ::: "memory" )

#endif /* PORTMACRO_H */
