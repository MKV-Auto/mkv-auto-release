import { TestBed } from '@angular/core/testing';
import { ToastService, formatHttpErrorDetail } from './toast.service';

describe('ToastService', () => {
  let service: ToastService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [ToastService],
    });
    service = TestBed.inject(ToastService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('show', () => {
    it('adds toast to toasts$', (done) => {
      service.toasts$.subscribe((list) => {
        if (list.length > 0) {
          expect(list[0].message).toBe('hello');
          expect(list[0].kind).toBe('info');
          expect(list[0].id).toBeDefined();
          done();
        }
      });
      service.show('hello');
    });
  });

  describe('formatHttpErrorDetail', () => {
    it('formats release_not_link_ready with missing fields', () => {
      const msg = formatHttpErrorDetail({
        error: {
          detail: { error: 'release_not_link_ready', missing: ['upc', 'cover_front_url'] },
        },
      })
      expect(msg).toContain('not ready to link')
      expect(msg).toContain('upc')
    })

    it('returns string detail as-is', () => {
      expect(formatHttpErrorDetail({ error: { detail: 'Not found' } })).toBe('Not found')
    })
  })

  describe('dismiss', () => {
    it('removes toast by id', (done) => {
      let idToDismiss: number | null = null;
      let done_called = false;
      const sub = service.toasts$.subscribe((list) => {
        if (done_called) return;
        if (list.length === 1 && idToDismiss == null) {
          idToDismiss = list[0].id;
          service.dismiss(list[0].id);
        } else if (list.length === 0 && idToDismiss != null) {
          expect(list).toEqual([]);
          done_called = true;
          sub.unsubscribe();
          done();
        }
      });
      service.show('a');
    });
  });
});
