const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('path');

let mainWindow;
let projectorWindow = null;

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      enableRemoteModule: true
    }
  });

  mainWindow.loadFile('index.html');
  mainWindow.webContents.openDevTools(); // For development

  mainWindow.on('closed', () => {
    mainWindow = null;
    if (projectorWindow) {
      projectorWindow.close();
    }
  });
}

// IPC handlers for dual-display mode
ipcMain.on('open-projector', (event, videoSrc) => {
  if (projectorWindow) {
    projectorWindow.focus();
    return;
  }

  const displays = screen.getAllDisplays();
  const externalDisplay = displays.find((display) => {
    return display.bounds.x !== 0 || display.bounds.y !== 0;
  });

  if (externalDisplay) {
    projectorWindow = new BrowserWindow({
      x: externalDisplay.bounds.x,
      y: externalDisplay.bounds.y,
      fullscreen: true,
      frame: false,
      webPreferences: {
        nodeIntegration: true,
        contextIsolation: false
      }
    });
  } else {
    // No external display, open fullscreen on main display
    projectorWindow = new BrowserWindow({
      fullscreen: true,
      frame: false,
      webPreferences: {
        nodeIntegration: true,
        contextIsolation: false
      }
    });
  }

  projectorWindow.loadFile('projector.html');

  projectorWindow.webContents.on('did-finish-load', () => {
    projectorWindow.webContents.send('set-video-src', videoSrc);
  });

  projectorWindow.on('closed', () => {
    projectorWindow = null;
  });
});

ipcMain.on('projector-play', () => {
  if (projectorWindow) {
    projectorWindow.webContents.send('play');
  }
});

ipcMain.on('projector-pause', () => {
  if (projectorWindow) {
    projectorWindow.webContents.send('pause');
  }
});

ipcMain.on('projector-seek', (event, time) => {
  if (projectorWindow) {
    projectorWindow.webContents.send('seek', time);
  }
});

ipcMain.on('close-projector', () => {
  if (projectorWindow) {
    projectorWindow.close();
    projectorWindow = null;
  }
});

app.whenReady().then(createMainWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createMainWindow();
  }
});
