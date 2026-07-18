// src/app/pages/ripper/services/error-recovery.service.spec.ts
import { TestBed } from '@angular/core/testing';
import { ErrorRecoveryService, ErrorType } from './error-recovery.service';
import { LoggerService } from '../../../services/logger.service';
import { throwError, of } from 'rxjs';

describe('ErrorRecoveryService', () => {
  let service: ErrorRecoveryService;
  let logger: jasmine.SpyObj<LoggerService>;

  beforeEach(() => {
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error']);

    TestBed.configureTestingModule({
      providers: [
        ErrorRecoveryService,
        { provide: LoggerService, useValue: loggerSpy },
      ],
    });

    service = TestBed.inject(ErrorRecoveryService);
    logger = TestBed.inject(LoggerService) as jasmine.SpyObj<LoggerService>;
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('classifyError', () => {
    it('should classify network errors as transient', () => {
      const error = { status: 0, statusText: 'Unknown Error' };
      expect(service.classifyError(error)).toBe(ErrorType.TRANSIENT);
    });

    it('should classify 5xx errors as transient', () => {
      const error = { status: 500 };
      expect(service.classifyError(error)).toBe(ErrorType.TRANSIENT);
    });

    it('should classify 4xx errors as permanent', () => {
      const error = { status: 404 };
      expect(service.classifyError(error)).toBe(ErrorType.PERMANENT);
    });

    it('should classify 408 and 429 as transient', () => {
      expect(service.classifyError({ status: 408 })).toBe(ErrorType.TRANSIENT);
      expect(service.classifyError({ status: 429 })).toBe(ErrorType.TRANSIENT);
    });

    it('should classify timeout errors as transient', () => {
      const error = { message: 'Request timeout' };
      expect(service.classifyError(error)).toBe(ErrorType.TRANSIENT);
    });
  });

  describe('getUserMessage', () => {
    it('should return user-friendly message for connection errors', () => {
      const error = { status: 0, statusText: 'Unknown Error', name: 'NetworkError' };
      const message = service.getUserMessage(error);
      expect(message.toLowerCase()).toContain('connect');
    });

    it('should return user-friendly message for 404 errors', () => {
      const error = { status: 404 };
      const message = service.getUserMessage(error);
      expect(message).toContain('not found');
    });

    it('should return user-friendly message for 403 errors', () => {
      const error = { status: 403 };
      const message = service.getUserMessage(error);
      expect(message).toContain('permission');
    });
  });

  describe('getErrorInfo', () => {
    it('should return complete error info', () => {
      const error = { status: 500, message: 'Server error' };
      const errorInfo = service.getErrorInfo(error);
      
      expect(errorInfo.type).toBe(ErrorType.TRANSIENT);
      expect(errorInfo.retryable).toBe(true);
      expect(errorInfo.originalError).toBe(error);
    });
  });

  describe('retryWithBackoff', () => {
    it('should retry on transient errors', (done) => {
      let attempts = 0;
      const source = throwError(() => {
        attempts++;
        return { status: 500 };
      });

      service.retryWithBackoff(source, 3, 10, 100).subscribe({
        next: () => {
          fail('Should not succeed');
        },
        error: () => {
          expect(attempts).toBeGreaterThan(1);
          done();
        },
      });
    });

    it('should not retry on permanent errors', (done) => {
      let attempts = 0;
      const source = throwError(() => {
        attempts++;
        return { status: 404 };
      });

      service.retryWithBackoff(source, 3, 10, 100).subscribe({
        next: () => {
          fail('Should not succeed');
        },
        error: () => {
          expect(attempts).toBe(1); // Should not retry
          done();
        },
      });
    });
  });
});

