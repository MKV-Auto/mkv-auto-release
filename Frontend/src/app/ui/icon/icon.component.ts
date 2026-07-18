import { ChangeDetectionStrategy, Component, Input, OnChanges } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ICON_PATHS, IconName } from './icon-paths';

@Component({
  selector: 'ui-icon',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="ui-icon" [attr.aria-label]="ariaLabel" [attr.aria-hidden]="ariaLabel ? null : true" [innerHTML]="svg"></span>`,
  styles: [`
    .ui-icon { display: inline-flex; align-items: center; justify-content: center; line-height: 0; }
    .ui-icon svg { display: block; }
  `],
})
export class IconComponent implements OnChanges {
  @Input({ required: true }) name!: IconName;
  @Input() size = 16;
  @Input() ariaLabel: string | null = null;

  svg: SafeHtml = '';

  constructor(private sanitizer: DomSanitizer) {}

  ngOnChanges(): void {
    const inner = ICON_PATHS[this.name] ?? '';
    const markup =
      `<svg width="${this.size}" height="${this.size}" viewBox="0 0 24 24" ` +
      `fill="none" stroke="currentColor" stroke-width="2" ` +
      `stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
    this.svg = this.sanitizer.bypassSecurityTrustHtml(markup);
  }
}
