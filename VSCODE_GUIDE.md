# How to Run LeafWatch in Visual Studio Code

This project is pre-configured with VS Code launch profiles and test discovery.

---

## 1. Open in VS Code

1. Extract `leafwatch_vscode.zip` (or open the existing directory `leafwatch`).
2. Open VS Code, select **File > Open Folder...**, and choose the `leafwatch` folder (the folder containing `app.py`).

---

## 2. Set Admin Key & Run

Before starting LeafWatch, set your secret admin key in your environment or terminal:

### In PowerShell:
```powershell
$env:LEAFWATCH_ADMIN_KEY = "your_private_admin_key"
```

### In Command Prompt:
```cmd
set LEAFWATCH_ADMIN_KEY=your_private_admin_key
```

Then press **F5** (or go to the **Run and Debug** tab in the sidebar `Ctrl+Shift+D`).
1. Ensure **LeafWatch: Run Server** is selected in the dropdown at the top.
2. Click the green Play button or press **F5**.
3. The backend server starts with hot-reload enabled:
   - Server URL: **http://127.0.0.1:8000**
4. Open your web browser to **http://127.0.0.1:8000** to use LeafWatch!

---

## 3. Run Automated Tests in VS Code

### Option A: Using VS Code Test Explorer
1. Click the **Testing** icon (the beaker) in the left sidebar (`Ctrl+Shift+T`).
2. VS Code will automatically discover all 77 tests in the `tests/` directory.
3. Click the **Play** button at the top of the test tree to run all tests with 1 click.

### Option B: Using the Terminal
Open the VS Code integrated terminal (`Ctrl+\``) and run:
```bash
python -m pytest tests -v
```

---

## 4. Alternate Run Options in VS Code Debug Dropdown

- **LeafWatch: Run Server (LAN / Mobile phones):** Binds to `0.0.0.0:8000` so mobile phones on the same Wi-Fi network can open `http://<YOUR-PC-IP>:8000`.
- **LeafWatch: Run All Pytest Tests:** Runs the full 77-test suite directly in the debugger.
- **LeafWatch: Audit Database Integrity:** Runs `check_integrity.py` to audit database constraints.
- **LeafWatch: Test Engine Sanity:** Runs `test_engine.py` for mathematical verification of fusion rules.

---

## 5. Command-Line / Batch Launcher Option

Set your admin key and run `run.bat`:
```cmd
set LEAFWATCH_ADMIN_KEY=your_private_admin_key
run.bat
```
If `LEAFWATCH_ADMIN_KEY` is not set, `run.bat` exits with a clear error prompt instructing you to configure it first.
