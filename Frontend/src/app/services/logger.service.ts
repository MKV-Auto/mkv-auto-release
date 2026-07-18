import { Injectable } from '@angular/core';
import { SystemService } from './system.service';
import { environment } from '../environments/environment';
import { HttpClient } from '@angular/common/http';

type LogLevel = 'ERROR' | 'WARNING' | 'INFO' | 'DEBUG';

@Injectable({ providedIn: 'root' })
export class LoggerService {
  private logLevel: LogLevel = (environment.logLevel || 'INFO').toUpperCase() as LogLevel;
  private logLevelMap: Record<LogLevel, number> = {
    ERROR: 40,
    WARNING: 30,
    INFO: 20,
    DEBUG: 10,
  };

  constructor(
    private systemService: SystemService,
    private http: HttpClient
  ) {
    // Check devmode status on initialization
    this.systemService.getDevMode().subscribe({
      next: status => {
        // Dev mode doesn't control logging anymore, but keep for compatibility
      },
      error: () => {
        // Ignore errors
      },
    });
  }

  private shouldLog(level: LogLevel): boolean {
    const configuredLevel = this.logLevelMap[this.logLevel] || 20;
    const messageLevel = this.logLevelMap[level] || 20;
    return messageLevel >= configuredLevel;
  }

  private formatTimestamp(): string {
    // Format timestamp to match backend format: YYYY-MM-DD HH:MM:SS
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
  }

  private getFacility(): string {
    // Extract facility from stack trace
    try {
      const stack = new Error().stack;
      if (stack) {
        const lines = stack.split('\n');
        // Skip first 3 lines (Error, getFacility, log method)
        // Look for the actual caller (skip Angular internals and logger methods)
        for (let i = 3; i < lines.length && i < 10; i++) {
          const callerLine = lines[i];
          // Skip Angular lifecycle hooks and change detection
          if (callerLine.includes('ngOnInit') || 
              callerLine.includes('ngAfterViewInit') || 
              callerLine.includes('ngOnChanges') ||
              callerLine.includes('ngDoCheck') ||
              callerLine.includes('checkAndUpdateView') ||
              callerLine.includes('detectChanges') ||
              callerLine.includes('LoggerService')) {
            continue;
          }
          // Try to extract file and function name
          const match = callerLine.match(/at\s+(\w+)\.(\w+)\s+\(([^)]+)\)/);
          if (match) {
            const [, className, methodName, filePath] = match;
            const fileName = filePath.split('/').pop()?.replace('.ts', '') || 'unknown';
            return `${fileName}:${methodName}`;
          }
        }
      }
    } catch {
      // Fallback if stack trace parsing fails
    }
    return 'unknown:unknown';
  }

  private async sendToBackend(level: LogLevel, facility: string, message: string): Promise<void> {
    // Skip sending logs for Angular lifecycle hooks to prevent hammering the backend
    const lifecycleHooks = ['ngOnInit', 'ngAfterViewInit', 'ngOnChanges', 'ngDoCheck', 'ngAfterContentInit', 'ngAfterContentChecked', 'ngAfterViewChecked'];
    if (lifecycleHooks.some(hook => facility.includes(hook))) {
      // Only log errors from lifecycle hooks, skip others
      if (level !== 'ERROR') {
        return;
      }
    }
    
    try {
      await this.http.post(`${environment.apiBase}/system/log`, {
        level,
        facility,
        message,
        timestamp: Date.now(),
      }).toPromise();
    } catch (error) {
      // Silently fail - don't log errors about logging
    }
  }

  private logInternal(level: LogLevel, ...args: any[]): void {
    if (!this.shouldLog(level)) {
      return;
    }

    const message = args.map(arg => 
      typeof arg === 'object' ? JSON.stringify(arg) : String(arg)
    ).join(' ');
    
    const facility = this.getFacility();
    const timestamp = this.formatTimestamp();
    const formattedMessage = `[${timestamp}] [${level}] ${facility} ${message}`;

    // Only ERROR and WARNING go to browser console
    // INFO and DEBUG only go to backend (per TODO requirement)
    switch (level) {
      case 'ERROR':
        console.error(formattedMessage, ...args);
        break;
      case 'WARNING':
        console.warn(formattedMessage, ...args);
        break;
      case 'INFO':
      case 'DEBUG':
        // INFO and DEBUG do not log to console - only to backend
        break;
    }

    if (environment.logToBackend) {
      // Send service logs to backend (async, don't wait)
      this.sendToBackend(level, facility, message).catch(() => {
        // Ignore errors
      });
    }
  }

  error(...args: any[]): void {
    this.logInternal('ERROR', ...args);
  }

  warn(...args: any[]): void {
    this.logInternal('WARNING', ...args);
  }

  info(...args: any[]): void {
    this.logInternal('INFO', ...args);
  }

  debug(...args: any[]): void {
    this.logInternal('DEBUG', ...args);
  }

  // Keep log() method for backward compatibility
  log(...args: any[]): void {
    this.info(...args);
  }
}







