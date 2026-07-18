import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { SetupStepTmdbComponent, TmdbStepData } from './setup-step-tmdb.component';
import { SystemService } from '../../../services/system.service';

describe('SetupStepTmdbComponent (#614)', () => {
  let component: SetupStepTmdbComponent;
  let fixture: ComponentFixture<SetupStepTmdbComponent>;
  let system: jasmine.SpyObj<SystemService>;
  let emittedChanges: Partial<TmdbStepData>[];

  function freshData(over: Partial<TmdbStepData> = {}): TmdbStepData {
    return { apiKeySet: false, apiKey: '', dismissed: false, ...over };
  }

  beforeEach(async () => {
    system = jasmine.createSpyObj<SystemService>('SystemService', ['saveTmdbConfig']);
    emittedChanges = [];
    await TestBed.configureTestingModule({
      imports: [SetupStepTmdbComponent],
      providers: [{ provide: SystemService, useValue: system }],
    }).compileComponents();
    fixture = TestBed.createComponent(SetupStepTmdbComponent);
    component = fixture.componentInstance;
    component.data = freshData();
    component.dataChange.subscribe(p => emittedChanges.push(p));
    component.ngOnChanges();
    fixture.detectChanges();
  });

  it('pre-populates the local input from data.apiKey on init', () => {
    component.data = freshData({ apiKey: 'pre-existing-key', apiKeySet: true });
    component.ngOnChanges();
    expect(component.localApiKey).toBe('pre-existing-key');
  });

  it('Save key button is disabled when input is empty or whitespace', () => {
    component.localApiKey = '   ';
    fixture.detectChanges();
    component.saveKey();
    expect(system.saveTmdbConfig).not.toHaveBeenCalled();
    expect(component.saving).toBe(false);
  });

  it('saveKey calls SystemService.saveTmdbConfig with the trimmed value', () => {
    system.saveTmdbConfig.and.returnValue(of({ api_key_set: true, api_key: 'tmdb-key-abc' } as any));
    component.localApiKey = '  tmdb-key-abc  ';
    component.saveKey();
    expect(system.saveTmdbConfig).toHaveBeenCalledOnceWith('tmdb-key-abc');
  });

  it('save success: marks saveResult success, emits apiKeySet=true + echoed key', () => {
    system.saveTmdbConfig.and.returnValue(of({ api_key_set: true, api_key: 'echoed-tmdb-key' } as any));
    component.localApiKey = 'echoed-tmdb-key';
    component.saveKey();
    expect(component.saving).toBe(false);
    expect(component.saveResult).toBe('success');
    const lastEmit = emittedChanges[emittedChanges.length - 1];
    expect(lastEmit.apiKey).toBe('echoed-tmdb-key');
    expect(lastEmit.apiKeySet).toBe(true);
    expect(lastEmit.dismissed).toBe(false);
  });

  it('save failure: marks saveResult error and surfaces backend detail', () => {
    system.saveTmdbConfig.and.returnValue(throwError(() => ({ error: { detail: 'Invalid TMDB key format' } })));
    component.localApiKey = 'bad-key';
    component.saveKey();
    expect(component.saving).toBe(false);
    expect(component.saveResult).toBe('error');
    expect(component.saveError).toBe('Invalid TMDB key format');
  });

  it('onKeyChange clears a stale save indicator + emits dismissed=false', () => {
    component.saveResult = 'success';
    component.saveError = null;
    component.onKeyChange('new-key');
    expect(component.saveResult).toBeNull();
    expect(emittedChanges[0]).toEqual({ apiKey: 'new-key', dismissed: false });
  });
});
