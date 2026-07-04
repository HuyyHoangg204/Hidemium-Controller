// ===================================
// POPUP SCRIPT - UI Control
// ===================================

class Logger {
    constructor(context) {
        this.context = context;
    }
    log(message, data = null) {
        if (data) console.log(`[${this.context}] ${message}`, data);
        else console.log(`[${this.context}] ${message}`);
    }
    debug(message, data = null) {
        if (data) console.log(`[${this.context}] ${message}`, data);
        else console.log(`[${this.context}] ${message}`);
    }
    warn(message, data = null) {
        if (data) console.warn(`[${this.context}] ⚠️ ${message}`, data);
        else console.warn(`[${this.context}] ⚠️ ${message}`);
    }
    error(message, data = null) {
        if (data) console.error(`[${this.context}] ❌ ${message}`, data);
        else console.error(`[${this.context}] ❌ ${message}`);
    }
    success(message, data = null) {
        if (data) console.log(`[${this.context}] ✅ ${message}`, data);
        else console.log(`[${this.context}] ✅ ${message}`);
    }
}

const logger = new Logger('PopupUI');

const elements = {
    enabled: document.getElementById('enabled'),
    autoReload: document.getElementById('autoReload'),
    reloadInterval: document.getElementById('reloadInterval'),
    clearGrecaptcha: document.getElementById('clearGrecaptcha'),
    serverUrl: document.getElementById('serverUrl'),
    operationMode: document.getElementById('operationMode'),
    saveSettings: document.getElementById('saveSettings'),
    reloadNow: document.getElementById('reloadNow'),
    autoReloadRow: document.getElementById('autoReloadRow'),
    intervalRow: document.getElementById('intervalRow'),
    serverUrlRow: document.getElementById('serverUrlRow'),
    operationModeRow: document.getElementById('operationModeRow')
};

// Load settings khi popup mở
async function loadSettings() {
    const response = await chrome.runtime.sendMessage({ type: 'GET_SETTINGS' });
    logger.log('Loaded settings', response);
    
    elements.enabled.checked = response.enabled;
    elements.autoReload.checked = response.autoReload;
    elements.reloadInterval.value = response.reloadInterval;
    elements.clearGrecaptcha.checked = response.clearGrecaptcha ?? false;
    elements.serverUrl.value = response.serverUrl || 'http://localhost:8080';
    elements.operationMode.value = response.operationMode || 'cookie';
    
    updateUIState();
}

// Save settings khi thay đổi (chỉ lưu, không reload)
async function saveSettings() {
    let url = elements.serverUrl.value.trim();
    if (!url) url = 'http://localhost:8080';
    // Remove trailing slash if exists
    if (url.endsWith('/')) url = url.slice(0, -1);
    // Ensure starts with http
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
        url = 'http://' + url;
    }
    elements.serverUrl.value = url;

    const settings = {
        enabled: elements.enabled.checked,
        autoReload: elements.autoReload.checked,
        reloadInterval: parseInt(elements.reloadInterval.value),
        clearGrecaptcha: elements.clearGrecaptcha.checked,
        serverUrl: url,
        operationMode: elements.operationMode.value
    };
    
    await chrome.runtime.sendMessage({
        type: 'SAVE_SETTINGS',
        settings: settings
    });
    
    logger.success('Settings saved', settings);
}

// Save và reload page
async function saveAndReload() {
    const button = elements.saveSettings;
    const originalHTML = button.innerHTML;
    
    button.innerHTML = '<span>💾</span><span>Saving...</span>';
    button.disabled = true;
    
    try {
        // Save settings
        await saveSettings();
        
        button.innerHTML = '<span>✅</span><span>Saved!</span>';
        
        // Haptic feedback
        if (navigator.vibrate) {
            navigator.vibrate([10, 50, 10]);
        }
        
        // Reload all active tabs
        setTimeout(async () => {
            const tabs = await chrome.tabs.query({ active: true });
            for (const tab of tabs) {
                try {
                    await chrome.tabs.reload(tab.id);
                } catch (error) {
                    logger.error('Failed to reload tab', error);
                }
            }
            
            button.innerHTML = originalHTML;
            button.disabled = false;
        }, 1000);
    } catch (error) {
        button.innerHTML = '<span>❌</span><span>Error</span>';
        
        setTimeout(() => {
            button.innerHTML = originalHTML;
            button.disabled = false;
        }, 2000);
    }
}

// Update UI state dựa vào settings
function updateUIState() {
    const enabled = elements.enabled.checked;
    const autoReload = elements.autoReload.checked;
    
    // Disable/enable controls
    elements.autoReload.disabled = !enabled;
    elements.reloadInterval.disabled = !enabled || !autoReload;
    elements.serverUrl.disabled = !enabled;
    
    // Visual feedback
    if (!enabled) {
        elements.autoReloadRow.classList.add('disabled');
        elements.intervalRow.classList.add('disabled');
        elements.serverUrlRow.classList.add('disabled');
    } else {
        elements.autoReloadRow.classList.remove('disabled');
        elements.serverUrlRow.classList.remove('disabled');
        
        if (autoReload) {
            elements.intervalRow.classList.remove('disabled');
        } else {
            elements.intervalRow.classList.add('disabled');
        }
    }
}

// Event listeners - Chỉ update UI, không save
elements.enabled.addEventListener('change', () => {
    updateUIState();
    
    // Add haptic feedback
    if (navigator.vibrate) {
        navigator.vibrate(10);
    }
});

elements.autoReload.addEventListener('change', () => {
    updateUIState();
    
    if (navigator.vibrate) {
        navigator.vibrate(10);
    }
});

// Save & Reload button
elements.saveSettings.addEventListener('click', async () => {
    await saveAndReload();
});

elements.reloadNow.addEventListener('click', async () => {
    const button = elements.reloadNow;
    const originalHTML = button.innerHTML;
    
    button.innerHTML = '<span>⏳</span><span>Reloading...</span>';
    button.disabled = true;
    
    try {
        await chrome.runtime.sendMessage({ type: 'RELOAD_NOW' });
        
        button.innerHTML = '<span>✅</span><span>Reloaded!</span>';
        
        // Haptic feedback
        if (navigator.vibrate) {
            navigator.vibrate([10, 50, 10]);
        }
        
        setTimeout(() => {
            button.innerHTML = originalHTML;
            button.disabled = false;
        }, 2000);
    } catch (error) {
        button.innerHTML = '<span>❌</span><span>Error</span>';
        
        setTimeout(() => {
            button.innerHTML = originalHTML;
            button.disabled = false;
        }, 2000);
    }
});

// Validate interval input
elements.reloadInterval.addEventListener('input', (e) => {
    let value = parseInt(e.target.value);
    if (isNaN(value) || value < 1) {
        e.target.value = 1;
    } else if (value > 60) {
        e.target.value = 60;
    }
});

// Prevent non-numeric input
elements.reloadInterval.addEventListener('keydown', (e) => {
    // Allow: backspace, delete, tab, escape, enter
    if ([46, 8, 9, 27, 13].indexOf(e.keyCode) !== -1 ||
        // Allow: Ctrl+A, Ctrl+C, Ctrl+V, Ctrl+X
        (e.keyCode === 65 && e.ctrlKey === true) ||
        (e.keyCode === 67 && e.ctrlKey === true) ||
        (e.keyCode === 86 && e.ctrlKey === true) ||
        (e.keyCode === 88 && e.ctrlKey === true) ||
        // Allow: home, end, left, right
        (e.keyCode >= 35 && e.keyCode <= 39)) {
        return;
    }
    // Ensure that it is a number and stop the keypress
    if ((e.shiftKey || (e.keyCode < 48 || e.keyCode > 57)) && (e.keyCode < 96 || e.keyCode > 105)) {
        e.preventDefault();
    }
});

// Initialize
loadSettings();