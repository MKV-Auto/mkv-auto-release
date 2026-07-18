import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { FieldComponent } from './field.component';

@Component({
  standalone: true,
  imports: [FieldComponent],
  template: `
    <ui-field [label]="label" [hint]="hint" [inline]="inline">
      <input uiFieldInline class="control-inline" />
      <input class="control-block" />
    </ui-field>
  `,
})
class HostComponent {
  label = 'Title';
  hint?: string;
  inline = false;
}

describe('FieldComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the label', () => {
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Title');
  });

  it('omits the hint when not provided', () => {
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-field__hint')).toBeNull();
  });

  it('shows the hint when provided', () => {
    host.hint = 'Required';
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).querySelector('.ui-field__hint')?.textContent?.trim()).toBe('Required');
  });

  it('projects default content as the block control', () => {
    const block = (fixture.nativeElement as HTMLElement).querySelector('.control-block');
    expect(block).toBeTruthy();
  });

  it('applies the inline modifier and projects the [uiFieldInline] slot when inline', () => {
    host.inline = true;
    fixture.detectChanges();
    const root = (fixture.nativeElement as HTMLElement).querySelector('.ui-field');
    expect(root?.classList.contains('ui-field--inline')).toBeTrue();
    const inlineControl = (fixture.nativeElement as HTMLElement).querySelector('.control-inline');
    expect(inlineControl).toBeTruthy();
  });
});
