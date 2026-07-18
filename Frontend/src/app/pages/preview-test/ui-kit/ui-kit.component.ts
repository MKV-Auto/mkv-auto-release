import { Component } from '@angular/core';
import {
  BtnComponent,
  CardComponent,
  ChipComponent,
  DriveCardComponent,
  type DriveCardData,
  EmptyStateComponent,
  FieldComponent,
  IconComponent,
  KbdComponent,
  LibraryCardComponent,
  PathChipComponent,
  PillComponent,
  ProgressRingComponent,
  SectionHeaderComponent,
  StageChipComponent,
  StepperComponent,
} from '../../../ui';
import { ICON_PATHS, type IconName } from '../../../ui/icon/icon-paths';
import { TitleRowComponent, type TitleRowStatus } from '../../../components/title-row/title-row.component';
import { TitleEditorComponent } from '../../../components/title-editor/title-editor.component';

@Component({
  selector: 'app-ui-kit-demo',
  standalone: true,
  imports: [
    BtnComponent,
    CardComponent,
    ChipComponent,
    DriveCardComponent,
    EmptyStateComponent,
    FieldComponent,
    IconComponent,
    KbdComponent,
    LibraryCardComponent,
    PathChipComponent,
    PillComponent,
    ProgressRingComponent,
    SectionHeaderComponent,
    StageChipComponent,
    StepperComponent,
    TitleRowComponent,
    TitleEditorComponent,
  ],
  template: `
    <div class="kit">
      <h1>UI Kit Demo</h1>
      <p class="kit__intro">
        Devmode-only canvas for the design system port. Visit
        <code>/preview-test/ui-kit</code> to walk every primitive +
        composite. Compare visually to <code>research/MKV Auto UI/screenshots/</code>.
      </p>

      <section>
        <h2>Tokens</h2>
        <div class="kit__row">
          <div class="swatch" style="background: var(--ui-bg-base);">--ui-bg-base</div>
          <div class="swatch" style="background: var(--ui-card-bg); border: 1px solid var(--ui-card-border);">--ui-card-bg</div>
          <div class="swatch" style="background: var(--ui-accent);">--ui-accent</div>
          <div class="swatch" style="background: var(--ui-success);">--ui-success</div>
          <div class="swatch" style="background: var(--ui-warning); color: #000;">--ui-warning</div>
          <div class="swatch" style="background: var(--ui-info);">--ui-info</div>
          <div class="swatch" style="background: var(--ui-danger);">--ui-danger</div>
        </div>
      </section>

      <section>
        <h2>Icons</h2>
        <div class="kit__icons">
          @for (n of iconNames; track n) {
            <div class="kit__icon">
              <ui-icon [name]="n" [size]="18" [ariaLabel]="n"></ui-icon>
              <code>{{ n }}</code>
            </div>
          }
        </div>
      </section>

      <section>
        <h2>Pills</h2>
        <div class="kit__row">
          <ui-pill tone="emerald">Emerald</ui-pill>
          <ui-pill tone="slate">Slate</ui-pill>
          <ui-pill tone="amber">Amber</ui-pill>
          <ui-pill tone="red">Red</ui-pill>
          <ui-pill tone="blue">Blue</ui-pill>
          <ui-pill tone="cyan">Cyan</ui-pill>
          <ui-pill tone="indigo">Indigo</ui-pill>
          <ui-pill tone="purple">Purple</ui-pill>
        </div>
      </section>

      <section>
        <h2>Buttons</h2>
        <div class="kit__row">
          <ui-btn variant="primary">Primary</ui-btn>
          <ui-btn variant="secondary">Secondary</ui-btn>
          <ui-btn variant="ghost">Ghost</ui-btn>
          <ui-btn variant="danger">Danger</ui-btn>
          <ui-btn variant="emerald">Emerald</ui-btn>
          <ui-btn variant="primary" [disabled]="true">Disabled</ui-btn>
          <ui-btn variant="primary" [loading]="true">Loading</ui-btn>
        </div>
      </section>

      <section>
        <h2>Cards</h2>
        <div class="kit__row">
          <ui-card>
            <div class="kit__pad">Default</div>
          </ui-card>
          <ui-card [active]="true">
            <div class="kit__pad">Active</div>
          </ui-card>
          <ui-card [interactive]="true">
            <div class="kit__pad">Interactive (click me)</div>
          </ui-card>
        </div>
      </section>

      <section>
        <h2>Field</h2>
        <ui-card>
          <div class="kit__pad">
            <ui-field label="Email" hint="We never share this.">
              <input class="kit__input" placeholder="user@example.com" />
            </ui-field>
            <ui-field label="Auto-rip on insert" [inline]="true">
              <input uiFieldInline type="checkbox" />
            </ui-field>
          </div>
        </ui-card>
      </section>

      <section>
        <h2>Progress ring</h2>
        <div class="kit__row" style="align-items: center; gap: 16px;">
          <ui-progress-ring [value]="0" [size]="40"></ui-progress-ring>
          <ui-progress-ring [value]="25" [size]="40"></ui-progress-ring>
          <ui-progress-ring [value]="60" [size]="40" tone="emerald"></ui-progress-ring>
          <ui-progress-ring [value]="80" [size]="40" tone="amber"></ui-progress-ring>
          <ui-progress-ring [value]="100" [size]="40" tone="indigo"></ui-progress-ring>
        </div>
      </section>

      <section>
        <h2>Stage chip</h2>
        <div class="kit__row">
          <ui-stage-chip status="pending" label="Queued" sub="0 / 12 titles"></ui-stage-chip>
          <ui-stage-chip status="active" label="Ripping" sub="t1.mkv (7.2 GB)"></ui-stage-chip>
          <ui-stage-chip status="done" label="Complete" sub="12 titles"></ui-stage-chip>
          <ui-stage-chip status="error" label="Failed" sub="Disk full"></ui-stage-chip>
          <ui-stage-chip status="skipped" label="Skipped"></ui-stage-chip>
        </div>
      </section>

      <section>
        <h2>Chip (filter toggle)</h2>
        <div class="kit__row">
          <ui-chip [active]="filterMovies" (toggled)="filterMovies = $event">Movies</ui-chip>
          <ui-chip [active]="filterSeries" (toggled)="filterSeries = $event">Series</ui-chip>
          <ui-chip [active]="filterBoxsets" (toggled)="filterBoxsets = $event">Boxsets</ui-chip>
        </div>
      </section>

      <section>
        <h2>Path chip</h2>
        <div class="kit__row" style="flex-direction: column; align-items: stretch;">
          <ui-path-chip path="/var/lib/mkv/movies/Inception (2010)/Inception (2010) - 1080p.mkv"></ui-path-chip>
          <ui-path-chip path="/very/long/path/with/many/segments/that/needs/rtl/ellipsis/treatment/file.mkv" [full]="true"></ui-path-chip>
        </div>
      </section>

      <section>
        <h2>Stepper</h2>
        <ui-stepper [steps]="['Film', 'Release', 'Disc', 'Titles']" [activeIndex]="2"></ui-stepper>
      </section>

      <section>
        <h2>Empty state</h2>
        <ui-empty-state title="No discs detected" body="Insert a disc and we'll scan it automatically.">
          <ui-icon uiEmptyIcon name="disc" [size]="20"></ui-icon>
          <ui-btn variant="primary">Refresh drives</ui-btn>
        </ui-empty-state>
      </section>

      <section>
        <h2>Section header</h2>
        <ui-card>
          <div class="kit__pad">
            <ui-section-header title="Drives" subtitle="Connected MakeMKV devices">
              <ui-icon uiSecIcon name="server" [size]="14"></ui-icon>
              <ui-btn variant="ghost">
                <ui-icon name="refresh" [size]="14"></ui-icon>
                Rescan
              </ui-btn>
            </ui-section-header>
          </div>
        </ui-card>
      </section>

      <section>
        <h2>Kbd</h2>
        <p>Press <ui-kbd>Esc</ui-kbd> to cancel, <ui-kbd>↵</ui-kbd> to confirm.</p>
      </section>

      <section>
        <h2>Drive card (state machine)</h2>
        <div class="kit__row" style="flex-wrap: wrap; gap: 16px;">
          @for (d of drives; track d.id) {
            <ui-drive-card [drive]="d" [active]="d.id === 'sr1'" (clicked)="onDrive($event)"></ui-drive-card>
          }
        </div>
      </section>

      <section>
        <h2>Library card</h2>
        <div class="kit__libgrid">
          <ui-library-card title="Inception" [year]="2010" resolution="4K" [completion]="100"></ui-library-card>
          <ui-library-card title="The Matrix" [year]="1999" resolution="1080p" [completion]="75" [active]="true"></ui-library-card>
          <ui-library-card title="Tenet" [year]="2020" resolution="4K"></ui-library-card>
          <ui-library-card title="Untitled placeholder release"></ui-library-card>
        </div>
      </section>

      <section>
        <h2>Title list + editor (mock data)</h2>
        <p class="kit__intro" style="margin-bottom: 12px;">
          Click a row to load it into the editor. Form changes mutate the
          mock title in-memory only — no autosave HTTP fires from this
          page; preview button is a stub.
        </p>
        <div class="kit__split">
          <div class="kit__split-list">
            @for (t of mockTitles; track t.title_id) {
              <app-title-row
                [title]="t.title"
                [sourceFile]="t.source_file"
                [duration]="t.duration_label"
                [status]="t.status"
                [progress]="t.progress"
                [selected]="selectedMockId === t.title_id"
                (selected$)="selectedMockId = t.title_id">
              </app-title-row>
            }
          </div>
          <div class="kit__split-editor">
            <app-title-editor
              [title]="selectedMock"
              [isSeries]="false"
              [showCloseButton]="true"
              [titleStatusFn]="mockStatusFn"
              [titleProgressValueFn]="mockProgressFn"
              [previewUrlFn]="mockPreviewUrlFn"
              [previewStateFn]="mockPreviewStateFn"
              [labelSaving]="false"
              [lastAutosaveOk]="true"
              (close)="selectedMockId = null"
              (titleChanged)="onMockTitleChanged()">
            </app-title-editor>
          </div>
        </div>
      </section>
    </div>
  `,
  styles: [`
    :host { display: block; padding: 24px; max-width: 1200px; margin: 0 auto; color: #fff; }
    h1 { font-size: 24px; font-weight: 700; margin: 0 0 4px; }
    h2 { font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: rgba(255,255,255,0.65); margin: 32px 0 12px; }
    .kit__intro { color: rgba(255,255,255,0.65); margin: 0 0 32px; max-width: 720px; }
    .kit__intro code { font-family: var(--ui-font-mono); background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px; }
    .kit__row { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start; }
    .kit__pad { padding: 16px; }
    .kit__icons { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
    .kit__icon { display: flex; align-items: center; gap: 8px; padding: 8px; background: rgba(255,255,255,0.04); border-radius: 8px; }
    .kit__icon code { font-family: var(--ui-font-mono); font-size: 11px; color: rgba(255,255,255,0.6); }
    .kit__input { all: unset; padding: 8px 10px; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; width: 100%; box-sizing: border-box; color: #fff; }
    .kit__libgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; max-width: 720px; }
    .swatch { width: 120px; height: 60px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; color: #fff; font-family: var(--ui-font-mono); }
    .kit__split {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(320px, 1.2fr);
      gap: 16px;
      align-items: stretch;
      min-height: 480px;
    }
    .kit__split-list {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 8px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 14px;
      overflow-y: auto;
      max-height: 600px;
    }
    .kit__split-editor { min-width: 0; }
    @media (max-width: 720px) {
      .kit__split { grid-template-columns: 1fr; }
    }
  `],
})
export class UiKitDemoComponent {
  readonly iconNames = Object.keys(ICON_PATHS) as IconName[];

  filterMovies = true;
  filterSeries = false;
  filterBoxsets = false;

  drives: DriveCardData[] = [
    { id: 'sr0', title: 'Inception (2010)', meta: 'Blu-ray · Disc 1', mount: '/dev/sr0', state: 'idle' },
    { id: 'sr1', title: 'Tenet (2020)', meta: 'UHD · Disc 1', mount: '/dev/sr1', state: 'canonical_ripping', progress: 42 },
    { id: 'sr2', title: 'Midway (2019)', meta: 'UHD · Disc 1', mount: '/dev/sr2', state: 'awaiting_user_choice', attention: true },
    { id: 'sr3', title: 'V for Vendetta', meta: 'UHD · Disc 1', mount: '/dev/sr3', state: 'matching_playlists' },
  ];

  onDrive(d: DriveCardData): void {
    // Demo only — log to keep the click handler honest.
    console.log('Drive clicked:', d.id);
  }

  // ── Mock title list + editor ─────────────────────────────────────────────
  // In-memory mock data only. Form changes mutate these objects but no
  // backend save fires. Refresh the page to reset.
  selectedMockId: string | null = 't1';
  mockTitles: Array<{
    title_id: string;
    title: string;
    type: string;
    duration: number;
    duration_label: string;
    size: number;
    chapters: number;
    source_file: string;
    description?: string;
    edition?: string;
    note?: string;
    season?: number | null;
    episode?: number | null;
    status: TitleRowStatus;
    progress: number | null;
  }> = [
    { title_id: 't1', title: 'Inception', type: 'movie', duration: 8400, duration_label: '2h 20m', size: 36 * 1024 ** 3, chapters: 18, source_file: '00539.mpls', status: 'complete', progress: 100, edition: '' },
    { title_id: 't2', title: 'Behind the Scenes', type: 'extra', duration: 1200, duration_label: '20m', size: 2 * 1024 ** 3, chapters: 0, source_file: '00012.mpls', status: 'pending', progress: null },
    { title_id: 't3', title: '', type: '', duration: 600, duration_label: '10m', size: 1024 ** 3, chapters: 0, source_file: '00099.mpls', status: 'running', progress: 47 },
    { title_id: 't4', title: 'Trailer', type: 'trailer', duration: 130, duration_label: '2m 10s', size: 200 * 1024 ** 2, chapters: 0, source_file: '00200.mpls', status: 'failed', progress: null },
    { title_id: 't5', title: 'Decoy variant 7', type: 'ignore', duration: 8400, duration_label: '2h 20m', size: 36 * 1024 ** 3, chapters: 18, source_file: '00541.mpls', status: 'ignored', progress: null },
    { title_id: 't6', title: 'V for Vendetta UHD', type: 'movie', duration: 8160, duration_label: '2h 16m', size: 64 * 1024 ** 3, chapters: 32, source_file: '00800.mpls', status: 'complete', progress: 100, description: 'Director\'s cut' },
  ];

  get selectedMock(): any | null {
    return this.mockTitles.find((t) => t.title_id === this.selectedMockId) ?? null;
  }

  /** Mock status callbacks — return data from the mockTitles list. */
  mockStatusFn = (id: string | null | undefined): string => {
    const t = this.mockTitles.find((m) => m.title_id === id);
    if (!t) return 'pending';
    return t.status === 'complete' ? 'completed'
      : t.status === 'running' ? 'running'
      : t.status === 'failed' ? 'failed'
      : 'pending';
  };
  mockProgressFn = (id: string | null | undefined): number => {
    const t = this.mockTitles.find((m) => m.title_id === id);
    return t?.progress ?? 0;
  };
  mockPreviewUrlFn = (_t: any): string | null => null;
  mockPreviewStateFn = (_t: any) => null;

  onMockTitleChanged(): void {
    // Mock save — in real wiring this is what triggers the backend autosave.
    // Here we just log to confirm the event path works.
    if (this.selectedMockId) {
      console.log('Mock title changed:', this.selectedMockId, this.selectedMock);
    }
  }
}
