import { Component, EventEmitter, Input, Output, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { forkJoin } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { of } from 'rxjs';
import {
  MakemkvStepData,
  SetupStepMakemkvComponent,
} from './steps/setup-step-makemkv.component';
import { SetupStepTransferComponent } from './steps/setup-step-transfer.component';
import { SetupStepPreviewComponent } from './steps/setup-step-preview.component';
import { SetupStepLibraryComponent } from './steps/setup-step-library.component';
import { SetupStepTmdbComponent, TmdbStepData } from './steps/setup-step-tmdb.component';
import { SetupStepDiscordComponent } from './steps/setup-step-discord.component';
import { SetupStepCompleteComponent } from './steps/setup-step-complete.component';
import { SystemService } from '../../services/system.service';
import { SetupModalConfig } from '../../services/setup-modal.service';
import { IconComponent } from '../../ui/icon/icon.component';
import { BtnComponent } from '../../ui/btn/btn.component';

export interface SetupStepData {
  makemkv: MakemkvStepData;
  transfer: { configured: boolean; configId?: string; configName?: string; configMode?: string; configPath?: string };
  preview: { duration: number; maxParallel: number };
  library: { type: 'plex' | 'jellyfin' };
  tmdb: TmdbStepData;
  discord: { enabled: boolean; webhookUrl: string; dismissed: boolean };
}

// #614: TMDB step (id 5) sits between Library and Discord. Discord renumbers
// to 6, Complete to 7. Existing 0.x users have first_time_setup_complete=true
// and bypass the modal — only new 1.0.0 installs see the new step.
const STEPS = [
  { id: 1, name: 'MakeMKV', description: 'Registration Key' },
  { id: 2, name: 'Transfer', description: 'Destination Setup' },
  { id: 3, name: 'Preview', description: 'Preview Settings' },
  { id: 4, name: 'Library', description: 'Library Type' },
  { id: 5, name: 'TMDB', description: 'Movie Database' },
  { id: 6, name: 'Discord', description: 'Notifications' },
  { id: 7, name: 'Complete', description: 'All Set!' },
];

const STEP_COUNT = STEPS.length; // 7

/**
 * Settings that, when supplied by the environment, make a step's question moot.
 * Steps absent from this map can never be answered by the environment: Transfer
 * destinations live in the database with encrypted credentials, and Complete is
 * not a question. A step counts as env-satisfied only if *every* listed key is
 * pinned — a half-configured Discord (webhook but no enable flag) still needs a
 * human.
 */
const STEP_ENV_KEYS: Record<number, string[]> = {
  1: ['makemkv_registration_key'],
  3: ['preview_duration_seconds', 'preview_max_parallel'],
  4: ['media_server'],
  5: ['tmdb_api_key'],
  6: ['discord.webhook_url', 'discord.enabled'],
};

@Component({
  selector: 'app-setup-modal',
  standalone: true,
  imports: [
    CommonModule,
    SetupStepMakemkvComponent,
    SetupStepTransferComponent,
    SetupStepPreviewComponent,
    SetupStepLibraryComponent,
    SetupStepTmdbComponent,
    SetupStepDiscordComponent,
    SetupStepCompleteComponent,
    IconComponent,
    BtnComponent,
  ],
  templateUrl: './setup-modal.component.html',
  styleUrls: ['./setup-modal.component.scss'],
})
export class SetupModalComponent implements OnInit {
  @Input() standalonePage = false;
  @Input() config?: SetupModalConfig;
  @Output() close = new EventEmitter<void>();
  @Output() complete = new EventEmitter<boolean>();

  readonly steps = STEPS;
  currentStep = 1;
  completedSteps: number[] = [];
  /** Dotted setting paths pinned by environment variables. */
  envManaged: string[] = [];
  loading = true;
  stepData: SetupStepData = {
    makemkv: {
      key: '',
      valid: false,
      installed: false,
      disc_workflow_blocked: false,
      disc_workflow_block_reason: 'none',
    },
    transfer: { configured: false },
    preview: { duration: 120, maxParallel: 5 },
    library: { type: 'plex' },
    tmdb: { apiKeySet: false, apiKey: '', dismissed: false },
    discord: { enabled: false, webhookUrl: '', dismissed: false },
  };

  constructor(private systemSvc: SystemService) {}

  ngOnInit(): void {
    forkJoin({
      setupStatus: this.systemSvc
        .getSetupStatus()
        .pipe(catchError(() => of({ first_time_setup_complete: false, setup_step: 1, env_managed: [] }))),
      makemkvReg: this.systemSvc.getRegistrationStatus().pipe(catchError(() => of({ expired: true, currentKey: null, message: null }))),
      makemkvHealth: this.systemSvc.getMakeMKVHealth().pipe(
        catchError(() =>
          of({
            installed: false,
            valid: false,
            can_rip: false,
            version: null,
            missing_components: [],
            error: null,
            disc_workflow_blocked: false,
            disc_workflow_block_reason: 'none' as const,
          })
        )
      ),
      preview: this.systemSvc.getPreviewConfig().pipe(catchError(() => of({ duration_seconds: 120, max_parallel: 5 }))),
      discord: this.systemSvc.getDiscordConfig().pipe(
        catchError(() => of({ webhook_url: null, enabled: false, notification_preferences: undefined }))
      ),
      mediaServer: this.systemSvc.getMediaServerConfig().pipe(catchError(() => of({ media_server: 'plex' as const }))),
      transferConfigs: this.systemSvc.getTransferConfigs().pipe(catchError(() => of([]))),
      tmdb: this.systemSvc.getTmdbConfig().pipe(catchError(() => of({ api_key_set: false, api_key: null }))),
    }).subscribe((result) => {
      this.loading = false;
      this.envManaged = result.setupStatus.env_managed ?? [];
      // Use targetStep from config if provided, otherwise use saved setup step
      const step = this.config?.targetStep ?? Math.max(1, Math.min(STEP_COUNT, result.setupStatus.setup_step));
      this.currentStep = step;
      this.completedSteps = Array.from({ length: step - 1 }, (_, i) => i + 1);

      const key = result.makemkvReg.currentKey ?? '';
      const isKeyValid = !result.makemkvReg.expired && !!key;
      const isInstalled = result.makemkvHealth.valid && result.makemkvHealth.can_rip;
      const discBlocked = result.makemkvHealth.disc_workflow_blocked === true;
      const discReason = result.makemkvHealth.disc_workflow_block_reason ?? 'none';

      this.stepData = {
        ...this.stepData,
        makemkv: {
          key,
          valid: isKeyValid,
          installed: isInstalled,
          disc_workflow_blocked: discBlocked,
          disc_workflow_block_reason: discReason,
        },
        preview: {
          duration: result.preview.duration_seconds ?? 120,
          maxParallel: result.preview.max_parallel ?? 5,
        },
        tmdb: {
          apiKeySet: !!result.tmdb?.api_key_set,
          apiKey: result.tmdb?.api_key ?? '',
          dismissed: false,
        },
        discord: {
          enabled: result.discord?.enabled ?? false,
          webhookUrl: result.discord?.webhook_url ?? '',
          dismissed: !result.discord?.enabled && !result.discord?.webhook_url,
        },
        library: { type: result.mediaServer.media_server === 'jellyfin' ? 'jellyfin' : 'plex' },
        transfer: (() => {
          const active = result.transferConfigs.find((c) => c.is_active);
          if (!active) return { configured: false };
          return {
            configured: true,
            configId: active.id,
            configName: active.name ?? undefined,
            configMode: active.mode,
            configPath: active.transfer_dir ?? undefined,
          };
        })(),
      };

      this.skipEnvSatisfiedSteps();
    });
  }

  /**
   * Land on the first step the environment has not already answered.
   *
   * An unattended deployment that pinned the MakeMKV and TMDB keys should not
   * open on a form asking for them. Env-satisfied steps are marked complete
   * rather than hidden, so the rail still shows what was configured and how far
   * along setup is — the user can click back into any of them to see the
   * (disabled) values the environment supplied.
   *
   * Not applied in targeted mode: the caller asked for a specific step.
   */
  private skipEnvSatisfiedSteps(): void {
    if (this.config?.targetStep || !this.envManaged.length) return;

    // Both conditions: the environment supplied the answer *and* the step is
    // genuinely satisfied. A pinned MakeMKV key must not skip past a MakeMKV
    // that failed to install — that is exactly the problem step 1 exists to show.
    while (
      this.currentStep < STEP_COUNT &&
      this.isStepEnvSatisfied(this.currentStep) &&
      this.isStepComplete(this.currentStep)
    ) {
      if (!this.completedSteps.includes(this.currentStep)) {
        this.completedSteps = [...this.completedSteps, this.currentStep];
      }
      this.currentStep++;
    }
    this.persistStep();
  }

  /** Every setting this step asks about is pinned by the environment. */
  isStepEnvSatisfied(step: number): boolean {
    const keys = STEP_ENV_KEYS[step];
    return !!keys?.length && keys.every((k) => this.envManaged.includes(k));
  }

  /** A field bound to this setting must be read-only — a restart would revert edits. */
  isEnvManaged(settingPath: string): boolean {
    return this.envManaged.includes(settingPath);
  }

  get canProceed(): boolean {
    return this.isStepComplete(this.currentStep);
  }

  isStepComplete(step: number): boolean {
    switch (step) {
      case 1: {
        const m = this.stepData.makemkv;
        const blocked = m.disc_workflow_blocked === true;
        return m.installed && (!blocked || m.valid);
      }
      case 2:
        return this.stepData.transfer.configured;
      case 3:
        return true;
      case 4:
        return true;
      // #614: TMDB step. Optional — complete if a key is saved OR user skipped.
      case 5:
        return this.stepData.tmdb.apiKeySet || this.stepData.tmdb.dismissed;
      // Env-satisfied counts as answered: a deployment that pins a webhook but
      // leaves `enabled` false is a deliberate choice, and without this the
      // wizard would dead-end on a step whose fields are disabled.
      case 6:
        return this.stepData.discord.enabled || this.stepData.discord.dismissed || this.isStepEnvSatisfied(6);
      case 7:
        return false;
      default:
        return false;
    }
  }

  canGoToStep(stepId: number): boolean {
    return this.completedSteps.includes(stepId) || stepId <= this.currentStep;
  }

  goToStep(stepId: number): void {
    if (this.canGoToStep(stepId)) {
      this.currentStep = stepId;
      this.persistStep();
    }
  }

  onBack(): void {
    if (this.currentStep > 1) {
      this.currentStep--;
      this.persistStep();
    }
  }

  onNext(): void {
    if (!this.canProceed) return;
    if (!this.completedSteps.includes(this.currentStep)) {
      this.completedSteps = [...this.completedSteps, this.currentStep];
    }
    
    // If we're in targeted mode and just completed the target step, close the modal
    if (this.config?.closeOnComplete && this.config?.targetStep === this.currentStep) {
      this.onClose();
      return;
    }
    
    if (this.currentStep < STEP_COUNT) {
      this.currentStep++;
      this.persistStep();
    }
  }

  private persistStep(): void {
    this.systemSvc.saveSetupProgress(this.currentStep).subscribe({ error: () => {} });
  }

  onStepDataChange(step: keyof SetupStepData, data: Partial<SetupStepData[keyof SetupStepData]>): void {
    const key = step as keyof SetupStepData;
    this.stepData = {
      ...this.stepData,
      [key]: { ...(this.stepData[key] as object), ...data } as SetupStepData[keyof SetupStepData],
    };
    // #689: the Library choice must reach the backend, not just wizard memory —
    // media_server drives postprocess naming paths, and the only other writer is
    // the Settings page. Persist on selection so Continue/Back/step-jumps/abandon
    // all keep the backend in sync with what the user picked.
    if (step === 'library') {
      const type = (data as Partial<SetupStepData['library']>).type;
      if (type === 'plex' || type === 'jellyfin') {
        this.systemSvc.saveMediaServerConfig({ media_server: type }).subscribe({ error: () => {} });
      }
    }
  }

  onComplete(showGuide: boolean): void {
    this.complete.emit(showGuide);
  }

  onClose(): void {
    this.close.emit();
  }

  skipDiscord(): void {
    this.onStepDataChange('discord', { dismissed: true });
    setTimeout(() => this.onNext(), 100);
  }

  // #614: Skip TMDB the same way Discord is skipped — mark dismissed and
  // advance. The user can still configure later via Settings → TMDB.
  skipTmdb(): void {
    this.onStepDataChange('tmdb', { dismissed: true });
    setTimeout(() => this.onNext(), 100);
  }
}
