import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  Component,
  Input,
  Output,
  EventEmitter,
} from '@angular/core';

import { Drive } from '../../services/drive.service';

@Component({
  selector: 'app-drive-selector',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './drive-selector.component.html',
})
export class DriveSelector {
  @Input() drives: Drive[] = [];
  @Input() selectedDrive: Drive | null = null;
  @Output() driveSelected = new EventEmitter<Drive>();

  onChange(discNum: string) {
    const drive = this.drives.find(d => d.disc_num === discNum);
    if (drive) {
      this.selectedDrive = drive;
      localStorage.setItem('preferred-drive', JSON.stringify(drive));
      this.driveSelected.emit(drive);
    }
  }
}
