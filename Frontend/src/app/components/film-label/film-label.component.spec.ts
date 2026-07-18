import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { FilmLabelComponent } from './film-label.component';
import { MetadataService } from '../../services/metadata.service';
import { LoggerService } from '../../services/logger.service';

describe('FilmLabelComponent', () => {
  let component: FilmLabelComponent;
  let fixture: ComponentFixture<FilmLabelComponent>;
  let metadataSvc: jasmine.SpyObj<MetadataService>;

  beforeEach(async () => {
    const metadataSpy = jasmine.createSpyObj('MetadataService', ['lookupMovie', 'getMovies', 'getMovie', 'createMovie']);
    metadataSpy.lookupMovie.and.returnValue(of({ name: 'Test Film', production_year: 2021, tmdb_id: 'tmdb1' }));
    metadataSpy.getMovies.and.returnValue(of([{ id: 'm1', tmdb_id: 'tmdb1', name: 'Test Film' } as any]));
    metadataSpy.getMovie.and.returnValue(of({ id: 'm1', name: 'Test Film', production_year: 2021 } as any));
    metadataSpy.createMovie.and.returnValue(of({ id: 'new1', name: 'Test Film' } as any));
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error']);

    await TestBed.configureTestingModule({
      imports: [FilmLabelComponent],
      providers: [
        { provide: MetadataService, useValue: metadataSpy },
        { provide: LoggerService, useValue: loggerSpy },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(FilmLabelComponent);
    component = fixture.componentInstance;
    metadataSvc = TestBed.inject(MetadataService) as jasmine.SpyObj<MetadataService>;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('updates filmData and displayFilmName from labelForm when film_name is set', () => {
    component.labelForm = { film_name: 'From Form', production_year: 2020 };
    fixture.detectChanges();
    expect(component.displayFilmName()).toBe('From Form · 2020');
  });

  it('emits filmSelected when lookup finds existing movie and updates form', () => {
    component.labelForm = {};
    component.tmdbUrl = 'https://www.themoviedb.org/movie/1';
    spyOn(component.filmSelected, 'emit');
    fixture.detectChanges();
    component.onLookup();
    expect(metadataSvc.lookupMovie).toHaveBeenCalledWith('https://www.themoviedb.org/movie/1');
    expect(component.filmSelected.emit).toHaveBeenCalledWith(
      jasmine.objectContaining({ film_id: 'm1', name: 'Test Film', production_year: 2021 })
    );
  });
});
