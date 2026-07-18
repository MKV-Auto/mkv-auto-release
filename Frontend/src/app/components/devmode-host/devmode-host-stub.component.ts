import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';

/**
 * Stub used when building with production-no-devmode: renders nothing.
 * Replaces DevmodeHostComponent via fileReplacements so the dev chunk is never loaded.
 * Exported as DevmodeHostComponent so shell import resolves.
 */
@Component({
  selector: 'app-devmode-host',
  standalone: true,
  imports: [CommonModule],
  template: '',
})
export class DevmodeHostComponent {}
