# DISABLED (Stage 9 hotfix B2b-06).
# Besides the facewinunlock-tauri provider, this script removed the registration of
# Microsoft's Windows Hello Face credential provider {8AF662BF-65A0-4D0A-A540-A338A999D36F}.
# It must not be used. To remove facewinunlock-tauri, uninstall it from Settings > Apps.
# To restore Windows Hello Face after an earlier run, see INSTALL.md section 6.

Write-Error "tools\disable_tauri.ps1 is disabled: it removed the Windows Hello Face registration. See INSTALL.md section 6."
exit 1
