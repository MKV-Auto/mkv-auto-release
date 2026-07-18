import { Component, Input, Output, EventEmitter, OnChanges, SimpleChanges, ViewChild, ElementRef, AfterViewInit, ChangeDetectorRef } from "@angular/core"
import { CommonModule } from "@angular/common"
import { FormsModule } from "@angular/forms"
import { MakeMKVUpdaterComponent } from "../components/makemkv-updater/makemkv-updater.component"
import { SystemService, StorageInfo, StorageDirEntry, RsyncConfig } from "../services/system.service"

@Component({
  selector: "app-settings-modal",
  standalone: true,
  imports: [CommonModule, FormsModule, MakeMKVUpdaterComponent],
  template: `
    <div [ngClass]="{'modal-backdrop': modalMode}">
      <div class="card modal-content w-[500px] max-w-[90%]">
        <div class="flex justify-between items-center mb-4">
          <h2 class="flex items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3"></circle>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
            </svg>
            Settings
          </h2>
          <button *ngIf="modalMode" class="bg-transparent border-0 text-2xl leading-none p-0 cursor-pointer" (click)="onClose()">&times;</button>
        </div>
        <div class="modal-body mb-4">
          <div class="card section-card">
            <label for="outputFolder" class="flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
              </svg>
              Output Folder
            </label>
            <div class="relative">
              <input 
                type="text" 
                id="outputFolder" 
                class="input pr-10"
                [(ngModel)]="localSettings.outputFolder" 
                placeholder="Enter output folder path">
              <button class="absolute right-0 top-0 h-full px-2.5 bg-transparent border-0 flex items-center justify-center text-gray-500 transition-colors hover:text-primary" (click)="openBrowser('output')">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                </svg>
              </button>
            </div>
            <div class="flex items-center gap-2 mt-2">
              <button class="secondary small" (click)="checkOutput()">Check space</button>
              <span class="muted text-xs">Local staging folder for rips/copies.</span>
            </div>
            <div *ngIf="storageInfo" class="muted text-sm mt-1">
              Path: {{ storageInfo.path }} — Free: {{ formatBytes(storageInfo.free) }} / Total: {{ formatBytes(storageInfo.total) }}
            </div>
            <div *ngIf="storageError" class="alert danger mt-1">{{ storageError }}</div>
          </div>

          <div class="card border border-gray-200 rounded-lg p-3 mb-3">
            <div class="flex items-center justify-between">
              <label for="transferFolder" class="flex items-center gap-2">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 3v18h18"></path><path d="M18 17H8V7"></path><path d="m18 9-4-4"></path>
                </svg>
                Transfer Destination
              </label>
              <div class="flex flex-col items-end gap-1">
                <span class="badge">
                  Mode: {{ transferMode === 'rsync' ? 'Remote (rsync)' : 'Local path' }}
                </span>
                <small class="muted text-xs" *ngIf="transferMode === 'local'">
                  Target: {{ localSettings.transferFolder || '(not set)' }}
                </small>
                <small class="muted text-xs" *ngIf="transferMode === 'rsync'">
                  Target: {{ (rsyncConfig.user || 'user') + '@' + (rsyncConfig.host || 'host') + ':' + (rsyncConfig.path || '') }}
                </small>
              </div>
            </div>
            <div class="input-with-icon">
              <input 
                type="text" 
                id="transferFolder" 
                [(ngModel)]="localSettings.transferFolder" 
                placeholder="Enter transfer destination path">
            </div>
            <div class="flex items-center gap-2 mt-2">
              <button class="secondary small" (click)="openTransferSetup()">{{ transferMode ? 'Reconfigure' : 'Setup' }} transfer destination</button>
              <button class="secondary small" (click)="checkTransfer()">Check space</button>
              <span class="muted text-xs">Select one method: local path or remote rsync.</span>
            </div>
            <div *ngIf="transferInfo" class="muted text-sm mt-1">
              Path: {{ transferInfo.path }} — Free: {{ formatBytes(transferInfo.free) }} / Total: {{ formatBytes(transferInfo.total) }}
            </div>
            <div *ngIf="transferError" class="alert danger mt-1">{{ transferError }}</div>

            <div class="muted text-sm mt-3" *ngIf="transferMode === 'rsync'">
              <strong>Configured rsync:</strong> {{ (rsyncConfig.user || 'user') + '@' + (rsyncConfig.host || 'host') + ':' + (rsyncConfig.path || '') }} (port {{ rsyncConfig.port || 22 }}) — Key: {{ hasRsyncKey ? 'present' : 'missing' }}
            </div>
          </div>

          <div class="card section-card">
            <label class="flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M3 3v18h18"></path><path d="M18 17H8V7"></path><path d="m18 9-4-4"></path>
              </svg>
              Storage Usage
            </label>
            <button class="secondary small" (click)="loadStorage()">Check defaults</button>
            <div *ngIf="storageInfo && transferInfo" class="muted text-sm">
              Output Free: {{ formatBytes(storageInfo.free) }} | Transfer Free: {{ formatBytes(transferInfo.free) }}
            </div>
            <div *ngIf="storageError" class="alert danger">{{ storageError }}</div>
          </div>
          <div class="mt-4">
            <app-makemkv-updater></app-makemkv-updater>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          <button (click)="onClose()">Cancel</button>
          <button class="primary" (click)="onSave()">Save</button>
        </div>
      </div>
    </div>

    <!-- Directory browser modal -->
    <div class="fixed inset-0 bg-black/50 flex items-center justify-center z-[1200]" *ngIf="showOutputPicker || showTransferPicker">
      <div class="bg-white p-4 rounded-lg w-[500px] max-h-[70vh] flex flex-col shadow-lg">
        <div class="flex justify-between items-center">
          <div>
            <strong>Select {{ showOutputPicker ? 'Output' : 'Transfer' }} Folder</strong>
            <div class="muted text-xs">{{ browserPath }}</div>
          </div>
          <div class="flex gap-2">
            <button class="secondary small" (click)="goUp()">Up</button>
            <button class="primary small" (click)="chooseCurrent()">Use This Folder</button>
            <button class="secondary small" (click)="closeBrowser(true)">Close</button>
          </div>
        </div>
        <div class="flex gap-2 mt-2">
          <input class="input flex-1" type="text" placeholder="New folder name" [(ngModel)]="newFolderName">
          <button class="secondary small" (click)="createFolder()">Create</button>
        </div>
        <div *ngIf="browserError" class="alert danger mt-2">{{ browserError }}</div>
        <div class="flex-1 overflow-auto mt-2 border border-gray-200 rounded-md p-2">
          <div *ngFor="let entry of browserEntries"
               class="py-1.5 px-2 rounded cursor-pointer hover:bg-gray-100"
               (click)="navigateInto(entry)">
            📁 {{ entry.name }}
          </div>
          <div *ngIf="browserEntries.length === 0" class="muted text-sm">No subdirectories</div>
        </div>
      </div>
    </div>

    <!-- Transfer setup wizard (renders beneath the directory browser when opened from wizard) -->
    <div class="browser-backdrop wizard-backdrop" *ngIf="showTransferWizard">
      <div class="browser" style="width: 560px;">
        <div class="flex justify-between items-center mb-2">
          <strong>Setup Transfer Destination</strong>
            <button class="secondary small" (click)="closeBrowser()">Close</button>
        </div>
        <div class="flex gap-2 mb-3">
          <button class="secondary small" [class.!bg-primary]="wizardTab==='local'" [class.!text-white]="wizardTab==='local'" (click)="wizardTab='local'">Local</button>
          <button class="secondary small" [class.!bg-primary]="wizardTab==='rsync'" [class.!text-white]="wizardTab==='rsync'" (click)="onRsyncTabClick()">Remote (rsync)</button>
        </div>

        <div class="card border border-gray-200 rounded-lg p-3 mb-4 mb-3" *ngIf="wizardTab==='local'">
          <div class="flex justify-between items-center">
            <div>
              <strong>Local / Mounted Path</strong>
              <div class="muted text-xs">Use a local/mounted folder for transfers.</div>
            </div>
            <button class="secondary small" (click)="openBrowser('transfer')">Browse</button>
          </div>
          <div class="muted text-xs mt-1">Current: {{ localSettings.transferFolder || '(not set)' }}</div>
          <div class="mt-2 flex gap-2">
            <button class="primary small" (click)="applyTransferMode('local')">Use local path</button>
            <button class="secondary small" (click)="closeWizard()">Close</button>
          </div>
        </div>

        <div class="card border border-gray-200 rounded-lg p-3 mb-4" *ngIf="wizardTab==='rsync'">
          <div class="flex justify-between items-center mb-2">
            <div>
              <strong>Remote (rsync over SSH)</strong>
              <div class="muted text-xs">Configure a remote target; key is stored server-side and not downloadable.</div>
            </div>
            <span class="badge" [class.success]="hasRsyncKey" [class.danger]="!hasRsyncKey">
              {{ hasRsyncKey ? 'Key uploaded' : 'Key missing' }}
            </span>
          </div>
          <div class="grid grid-cols-2 gap-2">
            <label class="flex flex-col gap-1 text-sm">
              Host
              <input class="input" type="text" [(ngModel)]="rsyncConfig.host" placeholder="example.com">
            </label>
            <label class="flex flex-col gap-1 text-sm">
              User
              <input class="input" type="text" [(ngModel)]="rsyncConfig.user" placeholder="ssh user">
            </label>
            <label class="flex flex-col gap-1 text-sm">
              Port
              <input class="input" type="number" min="1" max="65535" [(ngModel)]="rsyncConfig.port" placeholder="22">
            </label>
            <label class="flex flex-col gap-1 text-sm">
              Remote path
              <input class="input" type="text" [(ngModel)]="rsyncConfig.path" placeholder="/data/movies">
            </label>
            <label class="flex flex-col gap-1 text-sm">
              Bandwidth limit (KB/s, optional)
              <input class="input" type="number" min="1" [(ngModel)]="rsyncConfig.bwlimit" placeholder="50000">
            </label>
          </div>
          <div class="flex gap-2 mt-2 items-center">
            <button class="primary small" (click)="saveRsync()">Save rsync settings</button>
            <button class="secondary small" (click)="validateRsync()">Validate connection</button>
            <button class="secondary small" type="button" (click)="triggerFileInput()" [disabled]="uploadingKey">Upload SSH key</button>
            <input #fileInput type="file" style="display:none;" accept="" (change)="uploadRsyncKey($event)">
            <button class="secondary small" (click)="deleteRsyncKey()" [disabled]="!hasRsyncKey || uploadingKey">Remove key</button>
            <div class="flex items-center gap-1" *ngIf="uploadingKey || keyUploadSuccess">
              <svg *ngIf="uploadingKey" class="animate-spin" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 12a9 9 0 1 1-6.219-8.56"></path>
              </svg>
              <svg *ngIf="keyUploadSuccess && !uploadingKey" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #10b981;">
                <polyline points="20 6 9 17 4 12"></polyline>
              </svg>
            </div>
          </div>
          <div class="muted text-xs mt-1">We use rsync --partial with the uploaded key; you can replace the key anytime.</div>
          <div class="alert danger mt-2" *ngIf="rsyncError">{{ rsyncError }}</div>
          <div class="alert success mt-2" *ngIf="validationMessage">{{ validationMessage }}</div>
          <div class="alert success mt-2" *ngIf="rsyncSaved">{{ rsyncSaved }}</div>
          <div class="mt-2 flex gap-2">
            <button class="primary small" (click)="applyTransferMode('rsync')" [disabled]="!hasRsyncKey || !rsyncConfig.host || !rsyncConfig.user || !rsyncConfig.path">Use remote rsync</button>
            <button class="secondary small" (click)="closeWizard()">Close</button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class SettingsModal implements AfterViewInit {
  @Input() settings: any
  @Input() modalMode: boolean = true
  @Output() close = new EventEmitter<void>()
  @Output() save = new EventEmitter<any>()

  localSettings: any = {}
  transferMode: 'local' | 'rsync' = 'local'
  storageInfo: StorageInfo | null = null
  storageError: string | null = null
  transferInfo: StorageInfo | null = null
  transferError: string | null = null
  rsyncConfig: RsyncConfig = { host: '', user: '', path: '', port: 22, bwlimit: undefined }
  hasRsyncKey = false
  rsyncError: string | null = null
  rsyncSaved: string | null = null
  validationMessage: string | null = null
  uploadingKey = false
  keyUploadSuccess = false
  showOutputPicker = false
  showTransferPicker = false
  showTransferWizard = false
  wizardTab: 'local' | 'rsync' = 'local'
  browserPath: string = '/'
  browserEntries: StorageDirEntry[] = []
  browserError: string | null = null
  newFolderName: string = ''
  @ViewChild('fileInput') fileInputRef?: ElementRef<HTMLInputElement>

  constructor(private systemSvc: SystemService, private cdr: ChangeDetectorRef) {}

  ngAfterViewInit(): void {
    // Ensure accept attribute is removed when view initializes
    this.ensureAcceptRemoved()
  }

  private ensureAcceptRemoved(): void {
    // Use setTimeout to ensure ViewChild is available after conditional rendering
    setTimeout(() => {
      if (this.fileInputRef?.nativeElement) {
        const input = this.fileInputRef.nativeElement
        // Explicitly set accept to empty string to override any cached values
        const currentAccept = input.getAttribute('accept')
        if (currentAccept !== '') {
          input.setAttribute('accept', '')
        }
      }
    }, 0)
  }

  ngOnInit() {
    this.localSettings = { ...this.settings }
    this.transferMode = this.localSettings.transferMode || 'local'
    this.loadStorage()
    this.loadRsync()
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['settings'] && this.settings) {
      this.localSettings = { ...this.settings }
      this.transferMode = this.localSettings.transferMode || 'local'
    }
  }

  onClose(): void {
    this.close.emit()
  }

  onSave(): void {
    this.localSettings.transferMode = this.transferMode
    this.save.emit(this.localSettings)
  }
  
  loadStorage(): void {
    this.checkOutput()
    this.checkTransfer()
  }

  openBrowser(kind: 'output' | 'transfer'): void {
    this.browserError = null
    // If the transfer wizard is open, hide it while browsing so modals don’t overlap.
    this.showTransferWizard = false
    this.showOutputPicker = kind === 'output'
    this.showTransferPicker = kind === 'transfer'
    // start from current path or root
    const startPath = kind === 'output' ? this.localSettings.outputFolder : this.localSettings.transferFolder
    this.browserPath = startPath || '/'
    this.loadDir(this.browserPath)
  }

  navigateInto(entry: StorageDirEntry): void {
    this.browserPath = entry.path
    this.loadDir(this.browserPath)
  }

  chooseCurrent(): void {
    if (this.showOutputPicker) {
      this.localSettings.outputFolder = this.browserPath
      this.showOutputPicker = false
    } else if (this.showTransferPicker) {
      this.localSettings.transferFolder = this.browserPath
      this.transferMode = 'local'
      this.showTransferPicker = false
    }
    // auto-save selection to parent
    this.save.emit({ ...this.localSettings, transferMode: this.transferMode })
  }

  loadDir(path: string): void {
    this.systemSvc.listDirectory(path).subscribe({
      next: entries => {
        this.browserEntries = entries
        this.browserError = null
      },
      error: err => {
        this.browserError = err.error?.detail || err.message || 'Failed to list directory'
        this.browserEntries = []
      }
    })
  }

  goUp(): void {
    const p = this.browserPath || '/'
    const parent = p === '/' ? '/' : p.split('/').slice(0, -1).join('/') || '/'
    this.browserPath = parent
    this.loadDir(parent)
  }

  createFolder(): void {
    const name = (this.newFolderName || '').trim()
    if (!name) return
    this.systemSvc.makeDirectory(this.browserPath, name).subscribe({
      next: (entry: StorageDirEntry) => {
        this.newFolderName = ''
        this.loadDir(this.browserPath)
      },
      error: (err: any) => {
        this.browserError = err.error?.detail || err.message || 'Failed to create folder'
      }
    })
  }

  closeBrowser(keepWizard: boolean = false): void {
    this.showOutputPicker = false
    this.showTransferPicker = false
    this.browserEntries = []
    this.browserError = null
    this.newFolderName = ''
    if (!keepWizard) this.showTransferWizard = false
  }

  openTransferSetup(): void {
    this.validationMessage = null
    this.showTransferWizard = true
    this.wizardTab = this.transferMode || 'local'
    this.showTransferPicker = false
    this.showOutputPicker = false
    this.loadRsync()
    // Ensure accept is removed when wizard opens (file input is conditionally rendered)
    setTimeout(() => this.ensureAcceptRemoved(), 100)
  }

  checkOutput(): void {
    const path = this.localSettings.outputFolder
    this.systemSvc.getStorage(path).subscribe({
      next: info => {
        this.storageInfo = info
        this.storageError = null
      },
      error: err => {
        this.storageError = err.error?.detail || err.message || 'Failed to load storage info'
      }
    })
  }

  checkTransfer(): void {
    // Use storage summary endpoint which includes remote storage detection for active transfer configs
    this.systemSvc.getStorageSummary().subscribe({
      next: summary => {
        this.transferInfo = summary.transfer_root
        this.transferError = null
      },
      error: err => {
        this.transferError = err.error?.detail || err.message || 'Failed to load transfer storage info'
      }
    })
  }

  formatBytes(n: number | null | undefined): string {
    if (n == null) return 'Unknown';
    const units = ['B','KB','MB','GB','TB','PB'];
    let idx = 0;
    let val = n;
    while (val >= 1024 && idx < units.length -1) { val /= 1024; idx++; }
    return `${val.toFixed(1)} ${units[idx]}`;
  }

  loadRsync(): void {
    this.rsyncError = null
    this.rsyncSaved = null
    this.systemSvc.getRsyncConfig().subscribe({
      next: res => {
        this.hasRsyncKey = res.hasKey
        if (res.config) {
          this.rsyncConfig = { ...this.rsyncConfig, ...res.config }
        }
      },
      error: err => {
        this.rsyncError = err.error?.detail || err.message || 'Failed to load rsync settings'
      }
    })
  }

  saveRsync(): void {
    this.rsyncError = null
    this.rsyncSaved = null
    this.validationMessage = null
    this.systemSvc.saveRsyncConfig(this.rsyncConfig).subscribe({
      next: res => {
        this.hasRsyncKey = res.hasKey
        if (!res.hasKey) {
          // Refresh key status from server to ensure we have the latest state
          this.systemSvc.getRsyncConfig().subscribe({
            next: keyRes => {
              this.hasRsyncKey = keyRes.hasKey
              if (!keyRes.hasKey) {
                this.rsyncError = 'SSH key not uploaded. Please upload an SSH key before saving rsync settings.'
                return
              }
              this.rsyncSaved = 'Saved rsync settings'
              if (res.config) this.rsyncConfig = { ...this.rsyncConfig, ...res.config }
            },
            error: keyErr => {
              this.rsyncError = 'Failed to verify SSH key status. Please ensure an SSH key is uploaded.'
            }
          })
        } else {
          this.rsyncSaved = 'Saved rsync settings'
          if (res.config) this.rsyncConfig = { ...this.rsyncConfig, ...res.config }
        }
      },
      error: err => {
        this.rsyncError = err.error?.detail || err.message || 'Failed to save rsync settings'
      }
    })
  }

  triggerFileInput(): void {
    if (this.fileInputRef?.nativeElement) {
      const input = this.fileInputRef.nativeElement
      // Explicitly set accept to empty string to override any cached values
      input.setAttribute('accept', '')
      input.click()
    }
  }

  uploadRsyncKey(event: Event): void {
    const input = event.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    this.rsyncError = null
    this.rsyncSaved = null
    this.validationMessage = null
    this.uploadingKey = true
    this.keyUploadSuccess = false
    this.cdr.detectChanges() // Force change detection to show spinner immediately
    this.systemSvc.uploadRsyncKey(file).subscribe({
      next: res => {
        this.hasRsyncKey = res.hasKey
        this.rsyncSaved = 'SSH key uploaded'
        this.uploadingKey = false
        this.keyUploadSuccess = true
        // Hide checkmark after 3 seconds
        setTimeout(() => {
          this.keyUploadSuccess = false
        }, 3000)
      },
      error: err => {
        this.rsyncError = err.error?.detail || err.message || 'Failed to upload key'
        this.uploadingKey = false
        this.keyUploadSuccess = false
      }
    })
  }

  deleteRsyncKey(): void {
    this.systemSvc.deleteRsyncKey().subscribe({
      next: res => {
        this.hasRsyncKey = res.hasKey
        this.rsyncSaved = 'SSH key removed'
        this.validationMessage = null
      },
      error: err => {
        this.rsyncError = err.error?.detail || err.message || 'Failed to remove key'
      }
    })
  }

  validateRsync(): void {
    this.validationMessage = null
    this.rsyncError = null
    this.systemSvc.validateRsync(this.rsyncConfig).subscribe({
      next: res => {
        this.validationMessage = res.message || 'Connection succeeded'
      },
      error: err => {
        this.rsyncError = err.error?.detail || err.message || 'Validation failed'
      }
    })
  }

  applyTransferMode(mode: 'local' | 'rsync'): void {
    if (mode === 'rsync') {
      // Validate SSH key is uploaded before applying rsync mode
      this.validationMessage = null
      this.rsyncError = null
      
      // Always refresh key status from server to ensure we have the latest state
      this.systemSvc.getRsyncConfig().subscribe({
        next: res => {
          this.hasRsyncKey = res.hasKey
          if (!res.hasKey) {
            this.rsyncError = 'SSH key not uploaded. Please upload an SSH key before using remote rsync.'
            // Don't close wizard, stay in context so user can upload key
            return
          }
          // Key exists, proceed with applying transfer mode
          this.transferMode = mode
          this.localSettings.transferMode = mode
          this.save.emit({ ...this.localSettings, transferMode: mode })
          this.showTransferWizard = false
        },
        error: err => {
          this.rsyncError = 'Failed to verify SSH key status. Please ensure an SSH key is uploaded.'
          // Don't close wizard, stay in context
        }
      })
      return
    }
    
    // For local mode, proceed without key check
    this.transferMode = mode
    this.localSettings.transferMode = mode
    this.save.emit({ ...this.localSettings, transferMode: mode })
    this.showTransferWizard = false
  }

  closeWizard(): void {
    this.showTransferWizard = false
  }

  onRsyncTabClick(): void {
    this.wizardTab = 'rsync'
    // Ensure accept is removed when rsync tab is shown (file input is conditionally rendered)
    setTimeout(() => this.ensureAcceptRemoved(), 100)
  }
}
