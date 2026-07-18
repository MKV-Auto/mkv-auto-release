import { Injectable } from "@angular/core"

@Injectable({
  providedIn: "root",
})
export class SettingsService {
  private readonly STORAGE_KEY = "bluray-ripper-settings"

  constructor() {}

  getSettings(): any {
    const settings = localStorage.getItem(this.STORAGE_KEY)
    return settings ? JSON.parse(settings) : null
  }

  saveSettings(settings: any): void {
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(settings))
  }
}
