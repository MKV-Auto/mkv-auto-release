import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { combineLatest, Observable } from 'rxjs';
import { map } from 'rxjs/operators';
import { DiscMetadata, WorkflowService } from '../../../../services/workflow.service';
import { IconComponent } from '../../../../ui/icon/icon.component';
import { PillComponent } from '../../../../ui/pill/pill.component';

/** #603: Informational banner below the carousel that appears when the
 *  active drive card represents a disc already in the user's Library.
 *  Passive — frames the inserted disc as known-ripped and offers a single
 *  "Open in Library" link. The regular labeling + actions workflow stays
 *  visible underneath, so a deliberate re-rip is one click away through
 *  the standard flow. */
@Component({
  selector: 'app-already-in-library-card',
  standalone: true,
  imports: [CommonModule, RouterModule, IconComponent, PillComponent],
  templateUrl: './already-in-library-card.component.html',
  styleUrls: ['./already-in-library-card.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AlreadyInLibraryCardComponent {
  /** The DiscMetadata of the currently selected drive card, or null when
   *  the selection is a job, no card, or the card is not finalized. */
  finalized$: Observable<DiscMetadata | null>;

  constructor(private workflow: WorkflowService) {
    this.finalized$ = combineLatest([
      this.workflow.discs$,
      this.workflow.getSelectedCard$(),
    ]).pipe(
      map(([discs, selected]) => {
        if (!selected || selected.type !== 'drive') return null;
        const match = discs.find(d =>
          d.disc_state === 'in_drive' &&
          (d.mount_point === selected.id || d.disc_id === selected.id)
        );
        if (!match || match.finalized !== true) return null;
        return match;
      })
    );
  }

  /** Resolved card title — prefers the joined release name, falls back to
   *  movie_name / info_title so we never render an empty heading. */
  cardTitle(disc: DiscMetadata): string {
    return disc.finalized_release_name || disc.movie_name || disc.info_title || 'Already in Library';
  }
}
