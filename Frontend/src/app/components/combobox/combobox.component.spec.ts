import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { OverlayModule } from '@angular/cdk/overlay';
import { ComboboxComponent, ComboboxItem } from './combobox.component';
import { MobileService } from '../../services/mobile.service';

describe('ComboboxComponent', () => {
  let component: ComboboxComponent;
  let fixture: ComponentFixture<ComboboxComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ComboboxComponent, OverlayModule],
      providers: [{ provide: MobileService, useValue: { isMobile$: of(false) } }],
    }).compileComponents();
    fixture = TestBed.createComponent(ComboboxComponent);
    component = fixture.componentInstance;
    component.items = [
      { id: '1', name: 'A' },
      { id: '2', name: 'B' },
    ] as ComboboxItem[];
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('selectedItem returns item matching selectedItemId', () => {
    component.selectedItemId = '1';
    expect(component.selectedItem?.id).toBe('1');
    expect(component.selectedItem?.name).toBe('A');
  });

  it('filteredItems returns items when no search', () => {
    expect(component.filteredItems.length).toBeGreaterThan(0);
  });

  it('onSelectItem emits and closes', () => {
    let emitted: ComboboxItem | undefined;
    component.itemSelected.subscribe((v) => (emitted = v));
    component.onSelectItem(component.items[0]);
    expect(emitted?.id).toBe('1');
    expect(component.isOpen).toBe(false);
  });

  it('onClear emits and closes', () => {
    let emitted = false;
    component.itemCleared.subscribe(() => (emitted = true));
    component.onClear();
    expect(emitted).toBe(true);
    expect(component.isOpen).toBe(false);
  });

  it('onToggle toggles isOpen', () => {
    expect(component.isOpen).toBe(false);
    component.onToggle();
    expect(component.isOpen).toBe(true);
    component.onToggle();
    expect(component.isOpen).toBe(false);
  });

  it('onClose sets isOpen false', () => {
    component.isOpen = true;
    component.onClose();
    expect(component.isOpen).toBe(false);
  });

  it('onTmdbLookup emits for valid URL', () => {
    component.addMode = 'tmdb-url';
    component.internalTmdbUrl = 'https://www.themoviedb.org/movie/1';
    let emitted: string | undefined;
    component.tmdbUrlLookup.subscribe((v) => (emitted = v));
    component.onTmdbLookup();
    expect(emitted).toBe('https://www.themoviedb.org/movie/1');
    expect(component.tmdbUrlInvalid).toBe(false);
  });

  it('onTmdbLookup sets invalid and does not emit for invalid URL', () => {
    component.addMode = 'tmdb-url';
    component.internalTmdbUrl = 'http://example.com';
    let emitted = false;
    component.tmdbUrlLookup.subscribe(() => (emitted = true));
    component.onTmdbLookup();
    expect(emitted).toBe(false);
    expect(component.tmdbUrlInvalid).toBe(true);
  });
});
