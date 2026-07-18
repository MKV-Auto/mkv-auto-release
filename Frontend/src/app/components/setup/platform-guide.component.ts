import { Component, EventEmitter, Output } from '@angular/core';
import { CommonModule } from '@angular/common';

interface GuideSection {
  id: number;
  title: string;
  iconName: string;
  color: string;
  gradient: string;
  glow: string;
}

const GUIDE_SECTIONS: GuideSection[] = [
  {
    id: 1,
    title: 'How We Identify Discs',
    iconName: 'disc',
    color: '#3b82f6',
    gradient: 'linear-gradient(135deg, #3b82f6 0%, #2563eb 100%)',
    glow: 'rgba(59, 130, 246, 0.4)',
  },
  {
    id: 2,
    title: "When We Can't Identify Discs",
    iconName: 'search',
    color: '#8b5cf6',
    gradient: 'linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%)',
    glow: 'rgba(139, 92, 246, 0.4)',
  },
  {
    id: 3,
    title: 'Movie Selection & Creation',
    iconName: 'film',
    color: '#ec4899',
    gradient: 'linear-gradient(135deg, #ec4899 0%, #db2777 100%)',
    glow: 'rgba(236, 72, 153, 0.4)',
  },
  {
    id: 4,
    title: 'Boxset vs Release',
    iconName: 'package',
    color: '#f59e0b',
    gradient: 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
    glow: 'rgba(245, 158, 11, 0.4)',
  },
  {
    id: 5,
    title: 'How Titles Work',
    iconName: 'list',
    color: '#14b8a6',
    gradient: 'linear-gradient(135deg, #14b8a6 0%, #0d9488 100%)',
    glow: 'rgba(20, 184, 166, 0.4)',
  },
  {
    id: 6,
    title: 'What Editions Are For',
    iconName: 'star',
    color: '#eab308',
    gradient: 'linear-gradient(135deg, #eab308 0%, #ca8a04 100%)',
    glow: 'rgba(234, 179, 8, 0.4)',
  },
];

@Component({
  selector: 'app-platform-guide',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './platform-guide.component.html',
  styleUrls: ['./platform-guide.component.scss'],
})
export class PlatformGuideComponent {
  @Output() close = new EventEmitter<void>();

  readonly sections = GUIDE_SECTIONS;
  currentSection = 1;

  get section(): GuideSection {
    return this.sections.find(s => s.id === this.currentSection) ?? this.sections[0];
  }

  get isLastSection(): boolean {
    return this.currentSection === this.sections.length;
  }

  goTo(sectionId: number): void {
    this.currentSection = sectionId;
  }

  onBack(): void {
    if (this.currentSection > 1) {
      this.currentSection--;
    }
  }

  onNext(): void {
    if (this.currentSection < this.sections.length) {
      this.currentSection++;
    } else {
      this.close.emit();
    }
  }

  onClose(): void {
    this.close.emit();
  }
}
