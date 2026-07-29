import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Shown under a setup field the container's environment pins.
 *
 * The wizard skips steps the environment has already answered, but a user can
 * click back into one — and a field that looks editable there is a trap: the
 * edit saves, then the next restart re-applies the environment over it. Rather
 * than repeat the explanation in five step components, they share this.
 */
@Component({
  selector: 'app-env-managed-note',
  standalone: true,
  imports: [CommonModule],
  template: `
    <p class="env-managed-note">
      Set by <code>{{ variable }}</code> on this container. Change it in your
      Docker/Compose configuration and restart — edits here would be reverted.
    </p>
  `,
  styles: [`
    .env-managed-note {
      display: flex;
      flex-wrap: wrap;
      gap: 0.25rem;
      margin: 0.5rem 0 0;
      font-size: 0.75rem;
      line-height: 1.45;
      /* Amber, not the usual help grey: this is a constraint on what the user
         can do here, not a tip they can take or leave. */
      color: rgba(251, 191, 36, 0.85);
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 0.7rem;
      padding: 0 0.2rem;
      border-radius: 0.2rem;
      background: rgba(251, 191, 36, 0.12);
    }
  `],
})
export class EnvManagedNoteComponent {
  /** The variable that supplies it, e.g. `MKVAUTO_TMDB_API_KEY`. */
  @Input({ required: true }) variable!: string;
}
