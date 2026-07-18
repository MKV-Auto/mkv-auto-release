import { TestBed } from '@angular/core/testing';
import { DiscDbService } from './discdb.service';

describe('DiscDbService', () => {
  let service: DiscDbService;
  let fetchSpy: jasmine.Spy;

  beforeEach(() => {
    fetchSpy = jasmine.createSpy('fetch').and.stub();
    (window as unknown as { fetch: unknown }).fetch = fetchSpy;
    TestBed.configureTestingModule({
      providers: [DiscDbService],
    });
    service = TestBed.inject(DiscDbService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('search', () => {
    it('returns results array on 200', async () => {
      const results = [{ id: '1', title: 'A', type: 'movie', slug: 'a' }];
      fetchSpy.and.returnValue(
        Promise.resolve(new Response(JSON.stringify({ results }), { status: 200 }))
      );
      const out = await service.search('test');
      expect(out).toEqual(results);
      expect(fetchSpy).toHaveBeenCalled();
      const url = (fetchSpy.calls.mostRecent().args[0] as string);
      expect(url).toContain('/discdb/search');
      expect(url).toContain('q=test');
    });

    it('returns empty array when results is missing', async () => {
      fetchSpy.and.returnValue(
        Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      );
      const out = await service.search('x');
      expect(out).toEqual([]);
    });

    it('throws on 4xx', async () => {
      fetchSpy.and.returnValue(
        Promise.resolve(new Response('Not found', { status: 404 }))
      );
      await expectAsync(service.search('x')).toBeRejectedWithError(/Search failed \(404\)/);
    });

    it('throws on 5xx', async () => {
      fetchSpy.and.returnValue(
        Promise.resolve(new Response('Server error', { status: 500 }))
      );
      await expectAsync(service.search('x')).toBeRejectedWithError(/Search failed \(500\)/);
    });
  });

  describe('detail', () => {
    it('returns detail object on 200', async () => {
      const obj = { id: '1', title: 'A', type: 'movie', slug: 'a', synopsis: 'S' };
      fetchSpy.and.returnValue(
        Promise.resolve(new Response(JSON.stringify(obj), { status: 200 }))
      );
      const out = await service.detail('a');
      expect(out).toEqual(obj);
      const url = (fetchSpy.calls.mostRecent().args[0] as string);
      expect(url).toContain('/discdb/detail');
      expect(url).toContain('slug=a');
    });

    it('throws on 4xx with response text', async () => {
      fetchSpy.and.returnValue(
        Promise.resolve(new Response('NotFound', { status: 404 }))
      );
      await expectAsync(service.detail('x')).toBeRejectedWithError(/NotFound/);
    });

    it('throws on 5xx', async () => {
      fetchSpy.and.returnValue(
        Promise.resolve(new Response('', { status: 500 }))
      );
      await expectAsync(service.detail('x')).toBeRejectedWithError(/Detail failed \(500\)/);
    });
  });
});
