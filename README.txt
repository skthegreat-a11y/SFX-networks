SFX NETWORKS — PRE-LAUNCH UPGRADE

Files in this package:
- main.py: FastAPI backend with automatic file-category database migration.
- index.html: same SFX Networks interface, with Home and Folder Area tabs.
- app.js: message/activity expand-collapse controls, folder sorting and authenticated downloads.

WHAT CHANGED
1. Home tab keeps private messages, audio/video calling and the admin dashboard.
2. Messages show the latest 20 by default. Use Show all / Show latest 20 to expand or shorten the history.
3. ADMIN ACTIVITY LOG shows the latest 8 events by default. Use Show all / Show latest 8 to expand or shorten it.
4. Folder Area stores uploaded files in the existing SFX database and automatically sorts them into Documents, Images, Videos, Audio, Archives, Code and Other.
5. Each file has a Download button. Existing files are retained; their categories are assigned from their file extensions when the backend starts.
6. Existing sfx_network.db accounts, messages, files and sessions are preserved. The backend adds the category column automatically.

INSTALL
1. Stop the currently running SFX Networks server (Ctrl+C in PowerShell).
2. Make a backup copy of your existing sfx_network.db before replacing files.
3. Copy main.py, index.html and app.js into your SFX Networks project folder, replacing the old versions.
4. Start the app the same way as before:
   python -m uvicorn main:app --host 127.0.0.1 --port 8000
5. Open or refresh http://127.0.0.1:8000 and sign in.

IMPORTANT
- Keep sfx_network.db. Do not delete it: it contains your existing accounts, messages and uploaded files.
- Files are still stored in the SQLite database, not in separate physical folders. Folder Area is an organised category view inside the app.
- The file download route remains authenticated.
- Test with two employee accounts before launch. Do not expose the service publicly without HTTPS, backups, rate limiting and a production deployment review.
