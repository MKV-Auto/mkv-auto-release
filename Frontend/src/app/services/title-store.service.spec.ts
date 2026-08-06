import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { TitleStore, TitleStoreContextBridge } from './title-store.service';

/** Minimal in-memory context for the bridge. */
class FakeContext implements TitleStoreContextBridge {
  discKey = 'disc-1';
  titles: any[] = [];
  applied: any[] = [];
  getActiveDiscKey() { return this.discKey; }
  getActiveTitles() { return this.titles; }
  applyTitles(update: any) {
    this.applied.push(update);
    if (update.titles) this.titles = update.titles;
  }
  titleKey(row: any) {
    const key = row?.title_id ?? row?.source_file;
    if (!key) throw new Error('no key');
    return String(key);
  }
}

describe('TitleStore', () => {
  let store: TitleStore;
  let http: HttpTestingController;
  let ctx: FakeContext;

  beforeEach(() => {
    TestBed.configureTestingModule({ imports: [HttpClientTestingModule] });
    store = TestBed.inject(TitleStore);
    http = TestBed.inject(HttpTestingController);
    ctx = new FakeContext();
    ctx.titles = [
      { title_id: 'a', title: 'Alpha', title_seq: 4 },
      { title_id: 'b', title: 'Beta', title_seq: 1 },
    ];
    store.attach(ctx);
    store.learnRowSeqs(ctx.titles);
  });

  afterEach(() => http.verify());

  const flushOk = (req: any, id: string, fields: any, seq: number) =>
    req.flush({ titles_version: seq, result: {
      title_id: id, success: true,
      updated_title: { title_id: id, title_seq: seq, ...fields },
    } });

  describe('per-title write queue', () => {
    it('serializes writes to one title: the second departs only after the ack, with the acked version', () => {
      // The design-doc rule "one in-flight write per title" — two same-tick
      // writes used to leave with the same base_seq, and one always lost
      // (measured 7/7 on rc.3 before the backend lock).
      let r1: any = null, r2: any = null;
      store.enqueuePatch('disc-1', { title_id: 'a', title: 'First' } as any).subscribe(r => (r1 = r));
      store.enqueuePatch('disc-1', { title_id: 'a', type: 'Episode' } as any).subscribe(r => (r2 = r));

      // Only ONE request is on the wire.
      const first = http.expectOne(r => r.method === 'PATCH' && r.url.endsWith('/discs/disc-1/titles'));
      expect(first.request.body.base_seq).toBe(4);
      expect(first.request.body.title).toBe('First');
      http.expectNone(r => r.method === 'PATCH');
      flushOk(first, 'a', { title: 'First' }, 5);
      expect(r1?.result?.success).toBe(true);

      // The queued write departs now — carrying the version the ack taught us.
      const second = http.expectOne(r => r.method === 'PATCH');
      expect(second.request.body.base_seq).toBe(5);
      expect(second.request.body.type).toBe('Episode');
      expect(second.request.body.title).toBeUndefined(); // only the queued fields
      flushOk(second, 'a', { title: 'First', type: 'Episode' }, 6);
      expect(r2?.result?.success).toBe(true);
      expect(store.knownSeq('a')).toBe(6);
    });

    it('coalesces several queued edits into ONE follow-up write', () => {
      store.enqueuePatch('disc-1', { title_id: 'a', title: 'Typing' } as any).subscribe();
      let r2: any = null, r3: any = null;
      store.enqueuePatch('disc-1', { title_id: 'a', type: 'Extra' } as any).subscribe(r => (r2 = r));
      store.enqueuePatch('disc-1', { title_id: 'a', season: 3 } as any).subscribe(r => (r3 = r));

      flushOk(http.expectOne(r => r.method === 'PATCH'), 'a', { title: 'Typing' }, 5);

      const second = http.expectOne(r => r.method === 'PATCH');
      expect(second.request.body.type).toBe('Extra');
      expect(second.request.body.season).toBe(3);
      expect(second.request.body.base_seq).toBe(5);
      flushOk(second, 'a', { type: 'Extra', season: 3 }, 6);
      http.expectNone(r => r.method === 'PATCH'); // ONE follow-up, not two

      // Every coalesced caller gets the same final response.
      expect(r2?.result?.updated_title?.title_seq).toBe(6);
      expect(r3?.result?.updated_title?.title_seq).toBe(6);
    });

    it('does not serialize writes to different titles', () => {
      store.enqueuePatch('disc-1', { title_id: 'a', title: 'A1' } as any).subscribe();
      store.enqueuePatch('disc-1', { title_id: 'b', title: 'B1' } as any).subscribe();
      const reqs = http.match(r => r.method === 'PATCH');
      expect(reqs.length).toBe(2); // both on the wire concurrently
      flushOk(reqs[0], 'a', { title: 'A1' }, 5);
      flushOk(reqs[1], 'b', { title: 'B1' }, 2);
    });

    it('a stale first attempt retries inside the slot; the queued write still departs last with the final version', () => {
      store.enqueuePatch('disc-1', { title_id: 'a', title: 'User Text' } as any).subscribe();
      let r2: any = null;
      store.enqueuePatch('disc-1', { title_id: 'a', episode: 7 } as any).subscribe(r => (r2 = r));

      // First attempt: rejected stale (background bump to 9).
      const first = http.expectOne(r => r.method === 'PATCH');
      first.flush({ titles_version: 9, result: {
        title_id: 'a', success: false, error_code: 'stale_seq',
        current_title: { title_id: 'a', title: 'Bumped', title_seq: 9 },
      } });
      // Retry departs immediately (same slot) with the server's version and
      // the SAME user fields — the queued write stays queued.
      const retry = http.expectOne(r => r.method === 'PATCH');
      expect(retry.request.body.base_seq).toBe(9);
      expect(retry.request.body.title).toBe('User Text');
      flushOk(retry, 'a', { title: 'User Text' }, 10);

      // Only now does the queued write depart, on top of the retry's ack.
      const queued = http.expectOne(r => r.method === 'PATCH');
      expect(queued.request.body.base_seq).toBe(10);
      expect(queued.request.body.episode).toBe(7);
      flushOk(queued, 'a', { title: 'User Text', episode: 7 }, 11);
      expect(r2?.result?.success).toBe(true);
    });

    it('an HTTP error releases the slot and the queued write still departs', () => {
      let err: any = null;
      store.enqueuePatch('disc-1', { title_id: 'a', title: 'Doomed' } as any)
        .subscribe({ error: e => (err = e) });
      let r2: any = null;
      store.enqueuePatch('disc-1', { title_id: 'a', season: 2 } as any).subscribe(r => (r2 = r));

      http.expectOne(r => r.method === 'PATCH').error(new ProgressEvent('network'));
      expect(err).toBeTruthy();

      const queued = http.expectOne(r => r.method === 'PATCH');
      expect(queued.request.body.season).toBe(2);
      flushOk(queued, 'a', { season: 2 }, 5);
      expect(r2?.result?.success).toBe(true);
    });
  });

  describe('ack application', () => {
    it('a write ack learns seqs even when the disc is not the active context', () => {
      ctx.discKey = 'some-other-disc';
      store.applyPatchResults('disc-1', [
        { title_id: 'a', success: true,
          updated_title: { title_id: 'a', title: 'Elsewhere', title_seq: 8 } } as any,
      ], 8);
      expect(store.knownSeq('a')).toBe(8);            // queue safety
      expect(ctx.applied.length).toBe(0);             // content untouched
    });
  });
});
