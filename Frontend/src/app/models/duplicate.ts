/**
 * Duplicate title detection: when the backend provides duplicate_info on titles,
 * the UI shows group indicators, badges, member lists, and metadata tag badges.
 * See docs/DUPLICATE_DETECTION_UI.md and template DUPLICATE_DETECTION_UI.md.
 */

/** Scan-only metrics from backend (aligned with comparative diff_tags / ffprobe). */
export interface DuplicateMetrics {
  chaptersCount: number;
  subtitleTrackCount: number;
  subtitleLanguageCount: number;
  audioScore: number;
  audioLanguageCount: number;
  videoBitrate: number | null;
  videoPixels: number;
  scanUsable: boolean;
}

export interface DuplicateInfo {
  groupId: string;
  groupSize: number;
  /** When set, number of group members actually present in the current titles list (avoids showing "2 titles" when only one is visible). */
  effectiveGroupSize?: number;
  sameAs: string[];
  tags: string[];
  diffTags: string[];
  metrics?: DuplicateMetrics | null;
  confidence: 'high' | 'medium' | 'low';
}

export type TagCategory = 'audio' | 'video' | 'subs' | 'chapters' | 'quality' | 'diff' | 'metadata';

export interface ParsedTag {
  category: TagCategory;
  label: string;
  isDiff: boolean;
  isPositive: boolean;
  /** Diff tag is informational / symmetric (no winner); use neutral chip styling. */
  isNeutral: boolean;
  rawTag: string;
}

export interface GroupColor {
  name: string;
  color: string;
  glow: string;
}
