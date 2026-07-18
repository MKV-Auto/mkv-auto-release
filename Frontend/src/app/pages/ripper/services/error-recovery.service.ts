// src/app/pages/ripper/services/error-recovery.service.ts
import { Injectable } from '@angular/core';
import { Observable, throwError, timer, of } from 'rxjs';
import { retryWhen, delay, take, tap, catchError, switchMap } from 'rxjs/operators';
import { LoggerService } from '../../../services/logger.service';

export enum ErrorType {
  TRANSIENT = 'transient', // Network errors, timeouts - should retry
  PERMANENT = 'permanent', // 404, 403 - should not retry
  UNKNOWN = 'unknown', // Unknown errors - retry with caution
}

export interface ErrorInfo {
  type: ErrorType;
  message: string;
  userMessage: string;
  retryable: boolean;
  originalError: any;
}

@Injectable({
  providedIn: 'root'
})
export class ErrorRecoveryService {
  constructor(private logger: LoggerService) {}

  /**
   * Classify error type
   */
  classifyError(error: any): ErrorType {
    if (!error) return ErrorType.UNKNOWN;

    // Network errors are transient
    if (error.status === 0 || error.statusText === 'Unknown Error' || error.name === 'NetworkError') {
      return ErrorType.TRANSIENT;
    }

    // HTTP status codes
    const status = error.status ?? error.code;
    if (status) {
      // 5xx errors are transient
      if (status >= 500 && status < 600) {
        return ErrorType.TRANSIENT;
      }
      // 4xx errors are usually permanent (except 408, 429)
      if (status >= 400 && status < 500) {
        if (status === 408 || status === 429) {
          return ErrorType.TRANSIENT; // Timeout or rate limit
        }
        return ErrorType.PERMANENT;
      }
    }

    // Timeout errors are transient
    if (error.message?.includes('timeout') || error.message?.includes('Timeout')) {
      return ErrorType.TRANSIENT;
    }

    return ErrorType.UNKNOWN;
  }

  /**
   * Get user-friendly error message
   */
  getUserMessage(error: any): string {
    const type = this.classifyError(error);
    const status = error.status ?? error.code;

    switch (type) {
      case ErrorType.TRANSIENT:
        if (status === 0 || status === 'ECONNREFUSED') {
          return 'Unable to connect to server. Please check your connection.';
        }
        if (status === 408 || error.message?.includes('timeout')) {
          return 'Request timed out. Please try again.';
        }
        if (status === 429) {
          return 'Too many requests. Please wait a moment and try again.';
        }
        return 'A temporary error occurred. Please try again.';
      
      case ErrorType.PERMANENT:
        if (status === 404) {
          return 'The requested resource was not found.';
        }
        if (status === 403) {
          return 'You do not have permission to access this resource.';
        }
        if (status === 400) {
          return error.error?.detail || error.message || 'Invalid request. Please check your input.';
        }
        return error.error?.detail || error.message || 'An error occurred.';
      
      default:
        return error.message || 'An unexpected error occurred.';
    }
  }

  /**
   * Get error info object
   */
  getErrorInfo(error: any): ErrorInfo {
    const type = this.classifyError(error);
    return {
      type,
      message: error.message || String(error),
      userMessage: this.getUserMessage(error),
      retryable: type === ErrorType.TRANSIENT || type === ErrorType.UNKNOWN,
      originalError: error,
    };
  }

  /**
   * Retry with exponential backoff
   */
  retryWithBackoff<T>(
    source: Observable<T>,
    maxRetries: number = 3,
    initialDelay: number = 1000,
    maxDelay: number = 30000
  ): Observable<T> {
    return source.pipe(
      retryWhen(errors =>
        errors.pipe(
          switchMap((error, index) => {
            const errorInfo = this.getErrorInfo(error);
            
            // Don't retry permanent errors
            if (!errorInfo.retryable) {
              this.logger.error('[ErrorRecovery] Permanent error, not retrying', error);
              return throwError(() => error);
            }

            // Don't retry if max retries exceeded
            if (index >= maxRetries) {
              this.logger.error(`[ErrorRecovery] Max retries (${maxRetries}) exceeded`, error);
              return throwError(() => error);
            }

            // Calculate delay with exponential backoff
            const delayMs = Math.min(initialDelay * Math.pow(2, index), maxDelay);
            this.logger.warn(`[ErrorRecovery] Retrying in ${delayMs}ms (attempt ${index + 1}/${maxRetries})`, error);

            return timer(delayMs);
          }),
          take(maxRetries + 1)
        )
      )
    );
  }

  /**
   * Handle error with recovery strategy
   */
  handleError<T>(
    error: any,
    recoveryFn?: () => Observable<T>
  ): Observable<T> {
    const errorInfo = this.getErrorInfo(error);
    
    this.logger.error('[ErrorRecovery] Error occurred', {
      type: errorInfo.type,
      message: errorInfo.message,
      retryable: errorInfo.retryable,
      error,
    });

    // If recovery function provided and error is retryable, try recovery
    if (errorInfo.retryable && recoveryFn) {
      this.logger.log('[ErrorRecovery] Attempting recovery');
      return recoveryFn().pipe(
        catchError(recoveryError => {
          this.logger.error('[ErrorRecovery] Recovery failed', recoveryError);
          return throwError(() => error); // Return original error
        })
      );
    }

    return throwError(() => error);
  }

  /**
   * Wrap observable with error recovery
   */
  withRecovery<T>(
    source: Observable<T>,
    options: {
      maxRetries?: number;
      initialDelay?: number;
      maxDelay?: number;
      recoveryFn?: () => Observable<T>;
    } = {}
  ): Observable<T> {
    const {
      maxRetries = 3,
      initialDelay = 1000,
      maxDelay = 30000,
      recoveryFn,
    } = options;

    return this.retryWithBackoff(source, maxRetries, initialDelay, maxDelay).pipe(
      catchError(error => this.handleError(error, recoveryFn))
    );
  }
}

