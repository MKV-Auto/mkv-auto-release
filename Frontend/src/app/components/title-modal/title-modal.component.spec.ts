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
});
