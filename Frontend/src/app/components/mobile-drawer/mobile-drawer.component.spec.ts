import { ComponentFixture, TestBed } from '@angular/core/testing';
import { OverlayModule } from '@angular/cdk/overlay';
import { MobileDrawerComponent } from './mobile-drawer.component';

describe('MobileDrawerComponent', () => {
  let component: MobileDrawerComponent;
  let fixture: ComponentFixture<MobileDrawerComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MobileDrawerComponent, OverlayModule],
    }).compileComponents();
    fixture = TestBed.createComponent(MobileDrawerComponent);
    component = fixture.componentInstance;
    component.isOpen = false;
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('title input is used', () => {
    component.title = 'Drawer Title';
    fixture.detectChanges();
    expect(component.title).toBe('Drawer Title');
  });

  it('onClose emits close', () => {
    let emitted = false;
    component.close.subscribe(() => (emitted = true));
    component.onClose();
    expect(emitted).toBe(true);
  });
});
