import { ComponentFixture, TestBed } from '@angular/core/testing';
import { PathTemplateEditorComponent } from './path-template-editor.component';

describe('PathTemplateEditorComponent', () => {
  let component: PathTemplateEditorComponent;
  let fixture: ComponentFixture<PathTemplateEditorComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PathTemplateEditorComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(PathTemplateEditorComponent);
    component = fixture.componentInstance;
    component.sampleData = { movie_name: 'X', year: 2020, disc_number: 1 };
    fixture.detectChanges();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('onTemplateChange emits templateChange', () => {
    let emitted: string | undefined;
    component.templateChange.subscribe((v) => (emitted = v));
    component.template = '{movie_name}';
    component.onTemplateChange();
    expect(emitted).toBe('{movie_name}');
  });

  it('updatePreview resolves placeholders from sampleData', () => {
    component.template = '{movie_name} ({year})';
    component.sampleData = { movie_name: 'X', year: 2020 };
    component.updatePreview();
    expect(component.preview).toContain('X');
    expect(component.preview).toContain('2020');
  });

  it('getVariableDescription returns description for known variable', () => {
    expect(component.getVariableDescription('movie_name')).toBe('Movie name');
    expect(component.getVariableDescription('disc_number')).toContain('Disc number');
  });

  it('getVariableDescription returns variable name for unknown', () => {
    expect(component.getVariableDescription('unknown_var')).toBe('unknown_var');
  });
});
