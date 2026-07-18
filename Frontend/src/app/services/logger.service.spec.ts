import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { of } from 'rxjs';
import { LoggerService } from './logger.service';
import { SystemService } from './system.service';

describe('LoggerService', () => {
  let service: LoggerService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        LoggerService,
        {
          provide: SystemService,
          useValue: { getDevMode: () => of({ enabled: false, repo_url: '', branch: '', repo_path: '', export_root: '' }) },
        },
      ],
    });
    service = TestBed.inject(LoggerService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('error does not throw', () => {
    expect(() => service.error('err')).not.toThrow();
  });

  it('warn does not throw', () => {
    expect(() => service.warn('w')).not.toThrow();
  });

  it('info does not throw', () => {
    expect(() => service.info('i')).not.toThrow();
  });

  it('debug does not throw', () => {
    expect(() => service.debug('d')).not.toThrow();
  });

  it('log does not throw', () => {
    expect(() => service.log('l')).not.toThrow();
  });
});
