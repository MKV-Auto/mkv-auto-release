import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DiscCarouselComponent, CardType } from './disc-carousel.component';
import { DiscMetadata } from '../../services/workflow.service';

describe('DiscCarouselComponent', () => {
  let component: DiscCarouselComponent;
  let fixture: ComponentFixture<DiscCarouselComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DiscCarouselComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(DiscCarouselComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('allCards returns drive and job cards from discs', () => {
    component.discs = [
      { disc_state: 'in_drive', mount_point: '/mnt/x', disc_id: 'd1' } as DiscMetadata,
      { disc_state: 'unfinished', job_id: 'j1', disc_id: 'd2' } as DiscMetadata,
    ];
    const cards = component.allCards;
    expect(cards.length).toBe(2);
    expect(cards[0].type).toBe('drive');
    expect(cards[0].id).toBe('/mnt/x');
    expect(cards[1].type).toBe('job');
    expect(cards[1].id).toBe('j1');
  });

  it('isCardActive returns true when selectedCard matches', () => {
    component.selectedCard = { type: 'drive', id: '/mnt/x' };
    const card: CardType = { type: 'drive', id: '/mnt/x', data: {} as DiscMetadata };
    expect(component.isCardActive(card)).toBe(true);
    expect(component.isCardActive({ type: 'drive', id: '/other', data: {} as DiscMetadata })).toBe(false);
  });

  it('onCardClick emits cardSelected', () => {
    let emitted: { type: 'drive' | 'job'; id: string } | undefined;
    component.cardSelected.subscribe((v) => (emitted = v));
    const card: CardType = { type: 'job', id: 'j1', data: { disc_state: 'unfinished', job_id: 'j1' } as DiscMetadata };
    component.onCardClick(card);
    expect(emitted).toEqual({ type: 'job', id: 'j1' });
  });

  it('getDiscTitle returns movie_name or Insert Disc for drive', () => {
    expect(
      component.getDiscTitle({ disc_state: 'in_drive', disc_num: '1' } as DiscMetadata)
    ).toBe('Insert Disc');
    expect(
      component.getDiscTitle({ disc_state: 'in_drive', movie_name: 'X' } as DiscMetadata)
    ).toBe('X');
  });

  it('getDiscMeta returns year and format', () => {
    const d = { production_year: 2020, disc_format: 'UHD' } as DiscMetadata;
    expect(component.getDiscMeta(d)).toContain('2020');
    expect(component.getDiscMeta(d)).toContain('UHD');
  });
});
