import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TitleModalComponent } from './title-modal.component';

describe('TitleModalComponent', () => {
  let component: TitleModalComponent;
  let fixture: ComponentFixture<TitleModalComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TitleModalComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(TitleModalComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onClose emits close', () => {
    let emitted = false;
    component.close.subscribe(() => (emitted = true));
    component.onClose();
    expect(emitted).toBe(true);
  });

  it('formatDuration formats seconds', () => {
    expect(component.formatDuration(90)).toBe('1m');
    expect(component.formatDuration(3665)).toBe('1h 1m');
  });

  it('formatSize formats bytes', () => {
    expect(component.formatSize(1024 * 1024 * 500)).toContain('500');
    expect(component.formatSize(1024 * 1024 * 500)).toContain('MB');
  });

  it('Backdrop type forces the name to "Backdrop" and clears it on the way out', () => {
    component.title = { title_id: 't1', title: 'Old', type: null } as any;
    const seen: any[] = [];
    spyOn(component.titlePatched, 'emit').and.callFake((p: any) => {
      seen.push(p);
      return true as any;
    });
    component.onTypeChange('Backdrop');
    expect(component.title.title).toBe('Backdrop');
    expect(seen[0]).toEqual(jasmine.objectContaining({ type: 'Backdrop', title: 'Backdrop' }));
    expect(component.isBackdropLocked()).toBeTrue();
    component.onTypeChange('Featurette');
    expect(component.title.title).toBe('');
    expect(seen[1]).toEqual(jasmine.objectContaining({ type: 'Featurette', title: null }));
    expect(component.isBackdropLocked()).toBeFalse();
  });

  it('isIgnored returns true when type is ignore', () => {
    component.title = { type: 'ignore' };
    expect(component.isIgnored()).toBe(true);
    component.title = { type: 'MainMovie' };
    expect(component.isIgnored()).toBe(false);
  });

  it('openPreview sets previewTitle and previewUrl when url exists', () => {
    component.title = { id: 't1' };
    component.previewUrlFn = () => 'http://preview/1.m3u8';
    component.openPreview();
    expect(component.previewTitle).toEqual(component.title);
    expect(component.previewUrl).toBe('http://preview/1.m3u8');
  });

  it('closePreview clears preview', () => {
    component.previewTitle = {} as any;
    component.previewUrl = 'x';
    component.closePreview();
    expect(component.previewTitle).toBeNull();
    expect(component.previewUrl).toBeNull();
  });

  it('#701: the preview modal footer no longer has a redundant Close button', () => {
    component.title = { title_id: 't1', type: 'MainMovie' } as any;
    component.previewTitle = { title_id: 't1' } as any;
    component.previewUrl = 'http://preview/1.m3u8';
    fixture.detectChanges();
    const foot: HTMLElement | null = fixture.nativeElement.querySelector('.modal-foot');
    expect(foot).toBeTruthy();
    // The header ✕ remains the single close affordance; no button in the foot.
    expect(foot!.querySelector('button')).toBeNull();
  });
});
