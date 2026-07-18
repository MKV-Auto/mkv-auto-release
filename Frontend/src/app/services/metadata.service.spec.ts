/**
 * MetadataService Tests
 * 
 * Tests all metadata service functionality including:
 * - Movie CRUD operations
 * - Release CRUD operations
 * - Boxset CRUD operations
 * - Disc operations
 * - Create+Link methods
 */
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { MetadataService } from './metadata.service';
import { environment } from '../environments/environment';
import { LoggerService } from './logger.service';

describe('MetadataService', () => {
  let service: MetadataService;
  let httpMock: HttpTestingController;
  const apiUrl = environment.apiBase ?? 'http://localhost:8000';

  beforeEach(() => {
    const loggerSpy = jasmine.createSpyObj('LoggerService', ['log', 'warn', 'error', 'debug']);
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        MetadataService,
        { provide: LoggerService, useValue: loggerSpy },
      ]
    });
    service = TestBed.inject(MetadataService);
    httpMock = TestBed.inject(HttpTestingController);
    // Constructor now calls loadWorkflowOptions() which hits /discs/options
    httpMock.expectOne(`${apiUrl}/discs/options`).flush({
      movieOptions: [],
      boxsetOptions: [],
      releaseOptions: [],
      groupOptions: [],
    });
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('Movie operations', () => {
    it('should get movies', (done) => {
      const mockMovies = [
        { id: '1', name: 'Movie 1', production_year: 2020 },
        { id: '2', name: 'Movie 2', production_year: 2021 }
      ];

      service.getMovies().subscribe(movies => {
        expect(movies).toEqual(mockMovies);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/movies`);
      expect(req.request.method).toBe('GET');
      req.flush(mockMovies);
    });

    it('should get movie by id', (done) => {
      const mockMovie = { id: '1', name: 'Movie 1', production_year: 2020 };

      service.getMovie('1').subscribe(movie => {
        expect(movie).toEqual(mockMovie);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/movies/1`);
      expect(req.request.method).toBe('GET');
      req.flush(mockMovie);
    });

    it('should create movie', (done) => {
      const movieData = { name: 'New Movie', production_year: 2023 };
      const mockMovie = { id: '3', ...movieData };

      service.createMovie(movieData).subscribe(movie => {
        expect(movie).toEqual(mockMovie);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/movies`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual(movieData);
      req.flush(mockMovie);
    });
  });

  describe('Release operations', () => {
    it('should list releases', (done) => {
      const mockReleases = [
        { id: '1', slug: 'release-1', name: 'Release 1', total_discs: 1, completed_discs: 0, finalized_discs: 0 }
      ];

      service.listReleases().subscribe(releases => {
        expect(releases).toEqual(mockReleases);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases`);
      expect(req.request.method).toBe('GET');
      req.flush(mockReleases);
    });

    it('should get release by id', (done) => {
      const mockRelease = { id: '1', slug: 'release-1', name: 'Release 1', total_discs: 1, completed_discs: 0, finalized_discs: 0 };

      service.getRelease('1').subscribe(release => {
        expect(release).toEqual(mockRelease);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases/1`);
      expect(req.request.method).toBe('GET');
      req.flush(mockRelease);
    });
  });

  describe('Boxset operations', () => {
    it('should list boxsets', (done) => {
      const mockBoxsets = [
        { id: '1', slug: 'boxset-1', name: 'Boxset 1', year: 2020, release_count: 5 }
      ];

      service.listBoxsets().subscribe(boxsets => {
        expect(boxsets).toEqual(mockBoxsets);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases/boxsets`);
      expect(req.request.method).toBe('GET');
      req.flush(mockBoxsets);
    });

    it('should get boxset by id', (done) => {
      const mockBoxset = { id: '1', slug: 'boxset-1', name: 'Boxset 1', year: 2020, release_count: 5 };

      service.getBoxset('1').subscribe(boxset => {
        expect(boxset).toEqual(mockBoxset);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases/boxsets/1`);
      expect(req.request.method).toBe('GET');
      req.flush(mockBoxset);
    });

    it('should create boxset', (done) => {
      const boxsetData = { name: 'New Boxset', year: 2023 };
      const mockBoxset = { id: '2', slug: 'new-boxset', ...boxsetData, release_count: 0 };

      service.createBoxset(boxsetData).subscribe(boxset => {
        expect(boxset).toEqual(mockBoxset);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases/boxsets`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual(boxsetData);
      req.flush(mockBoxset);
    });
  });

  describe('Create+Link methods', () => {
    it('should create and link movie', (done) => {
      const movieData = { name: 'New Movie', production_year: 2023 };
      const mockResult = { movie: { id: '3', ...movieData }, linked: false };

      service.createAndLinkMovie(movieData, 'context1', 'job').subscribe(result => {
        expect(result.movie).toBeDefined();
        expect(result.linked).toBe(true);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/movies`);
      expect(req.request.method).toBe('POST');
      req.flush({ id: '3', ...movieData });
    });

    it('should create and link release', (done) => {
      const releaseData = { slug: 'new-release', movie_id: '1' };
      const mockRelease = { id: 'r1', slug: 'new-release', total_discs: 1, completed_discs: 0, finalized_discs: 0 };

      service.createAndLinkRelease(releaseData, 'ctx', 'job').subscribe(result => {
        expect(result.release).toEqual(mockRelease);
        expect(result.linked).toBe(true);
        done();
      });

      const req = httpMock.expectOne(`${apiUrl}/releases`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual(releaseData);
      req.flush(mockRelease);
    });

    it('should create and link boxset', (done) => {
      const boxsetData = { name: 'New Boxset', year: 2023 };
      const mockBoxset = { id: 'b1', slug: 'new-boxset', name: 'New Boxset', year: 2023, release_count: 0 };

      service.createAndLinkBoxset(boxsetData, 'ctx', 'job').subscribe(result => {
        expect(result.boxset).toEqual(mockBoxset);
        expect(result.linked).toBe(true);
        done();
      });

      const req = httpMock.expectOne(r => r.url === `${apiUrl}/releases/boxsets` && r.method === 'POST');
      expect(req.request.body).toEqual(boxsetData);
      req.flush(mockBoxset);
    });
  });

  describe('error handling', () => {
    it('getBoxset should error on 404', (done) => {
      service.getBoxset('missing').subscribe({
        next: () => { done.fail('expected error'); },
        error: (err) => {
          expect(err.status).toBe(404);
          done();
        },
      });

      const req = httpMock.expectOne((r) => (r.url ?? '').includes('/releases/boxsets/missing'));
      req.flush('', { status: 404, statusText: 'Not Found' });
    });
  });
});
