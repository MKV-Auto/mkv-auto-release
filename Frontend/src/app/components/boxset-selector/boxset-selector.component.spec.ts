// src/app/components/boxset-selector/boxset-selector.component.spec.ts
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { BoxsetSelectorComponent } from './boxset-selector.component';
import { MetadataService } from '../../services/metadata.service';
import { of } from 'rxjs';
import { BoxsetSummary } from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';
import { MobileService } from '../../services/mobile.service';
import { ToastService } from '../../services/toast.service';

describe('BoxsetSelectorComponent', () => {
  let component: BoxsetSelectorComponent;
  let fixture: ComponentFixture<BoxsetSelectorComponent>;
  let metadataSvc: jasmine.SpyObj<MetadataService>;

  beforeEach(async () => {
    const metadataSpy = jasmine.createSpyObj('MetadataService', [
      'listBoxsets',
      'searchBoxsetsBackend',
      'updateBoxset',
      'deleteBoxset',
    ]);
    metadataSpy.searchBoxsetsBackend.and.returnValue(of([]));
    metadataSpy.updateBoxset.and.returnValue(of({} as BoxsetSummary));
    metadataSpy.deleteBoxset.and.returnValue(of({}));
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error']);
    const mobileStub = { isMobile$: of(false) };
    const toastSpy = jasmine.createSpyObj('ToastService', ['show']);

    await TestBed.configureTestingModule({
      imports: [BoxsetSelectorComponent],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: LoggerService, useValue: loggerSpy },
        { provide: MobileService, useValue: mobileStub },
        { provide: ToastService, useValue: toastSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(BoxsetSelectorComponent);
    component = fixture.componentInstance;
    metadataSvc = TestBed.inject(MetadataService) as jasmine.SpyObj<MetadataService>;
  });

  describe('#821: the create form keeps what the user typed', () => {
    const typed = {
      name: 'Star Wars: The Clone Wars - Seasons 1-5',
      year: 2014,
      upc: '786936841234',
      asin: 'B00EXAMPLE',
      cover_front_url: '',
      cover_back_url: '',
    };

    it('survives the panel closing and reopening', () => {
      component.openPanel();
      component.openCreateForm();
      component.onCreateDraftChanged({ ...typed });

      component.closePanel();
      component.openPanel();

      expect(component.showCreateForm).toBeTrue();
      expect(component.createFormPrefill).toEqual(jasmine.objectContaining({
        name: typed.name, year: 2014, upc: typed.upc, asin: typed.asin,
      }));
    });

    it('re-seeds by bumping resetVersion, without aliasing the draft', () => {
      component.openCreateForm();
      component.onCreateDraftChanged({ ...typed });
      const before = component.editionFormResetVersion;

      component.closePanel();
      component.openPanel();

      expect(component.editionFormResetVersion).toBeGreaterThan(before);
      expect(component.createFormPrefill).not.toBe(component.createDraft as any);
    });

    it('does not reopen the create form for an untouched one', () => {
      component.openCreateForm();
      component.onCreateDraftChanged({
        name: '', year: null, upc: '', asin: '', cover_front_url: '', cover_back_url: '',
      });

      component.closePanel();
      component.openPanel();

      expect(component.createDraft).toBeNull();
      expect(component.showCreateForm).toBeFalse();
    });

    it('cancel discards the draft', () => {
      component.openCreateForm();
      component.onCreateDraftChanged({ ...typed });

      component.cancelCreateForm();

      expect(component.createDraft).toBeNull();
      expect(component.showCreateForm).toBeFalse();
    });

    it('submitting the create discards the draft', () => {
      component.openCreateForm();
      component.onCreateDraftChanged({ ...typed });

      component.onBoxsetEditionCreate({ ...typed } as any);

      expect(component.createDraft).toBeNull();
    });
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('should load boxsets on init if options are empty', () => {
    const mockBoxsets: BoxsetSummary[] = [
      { id: '1', slug: 'boxset-1', name: 'Boxset 1', year: 2020, release_count: 1 },
    ];

    metadataSvc.listBoxsets.and.returnValue(of(mockBoxsets));
    component.boxsetOptions = [];

    component.ngOnInit();

    expect(metadataSvc.listBoxsets).toHaveBeenCalled();
  });

  it('should emit boxsetSelected when boxset is selected and link-ready', () => {
    const boxset: BoxsetSummary = {
      id: '1',
      slug: 'test-boxset',
      name: 'Test Boxset',
      year: 2020,
      release_count: 1,
      boxset_link_ready: true,
    };
    spyOn(component.boxsetSelected, 'emit');
    component.boxsetOptions = [boxset];
    component.isOpen = true;

    component.selectBoxset(boxset);

    expect(component.boxsetSelected.emit).toHaveBeenCalledWith(boxset);
    expect(component.isOpen).toBe(false);
  });

  it('should open pending edit when boxset is incomplete', () => {
    const boxset: BoxsetSummary = {
      id: '1',
      slug: 'test-boxset',
      name: 'Test Boxset',
      year: 2020,
      release_count: 1,
      boxset_link_ready: false,
    };
    spyOn(component.boxsetSelected, 'emit');
    component.boxsetOptions = [boxset];
    component.isOpen = true;

    component.selectBoxset(boxset);

    expect(component.showPendingEdit).toBe(true);
    expect(component.pendingEditBoxset).toBe(boxset);
    expect(component.boxsetSelected.emit).not.toHaveBeenCalled();
    expect(component.isOpen).toBe(true);
  });

  it('should emit boxsetToggled when cleared', () => {
    spyOn(component.boxsetToggled, 'emit');
    component.onBoxsetCleared();

    expect(component.boxsetToggled.emit).toHaveBeenCalledWith(false);
  });

  it('should clean up on destroy', () => {
    spyOn(component['destroy$'], 'next');
    spyOn(component['destroy$'], 'complete');

    component.ngOnDestroy();

    expect(component['destroy$'].next).toHaveBeenCalled();
    expect(component['destroy$'].complete).toHaveBeenCalled();
  });
});

