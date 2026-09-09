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
    flowSessionSyncEnabled: document.getElementById('flowSessionSyncEnabled'),
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
    if (response.success === false) throw new Error('Invalid settings');
    
    elements.enabled.checked = response.enabled;
    elements.flowSessionSyncEnabled.checked = response.flowSessionSyncEnabled === true;
    document.getElementById('flowCollectorKeyStatus').textContent = response.hasFlowCollectorKey ? 'Đã cấu hình khóa cho máy chủ này.' : 'Chưa có khóa collector cho máy chủ này.';
    elements.autoReload.checked = response.autoReload;
    elements.reloadInterval.value = response.reloadInterval;
    elements.clearGrecaptcha.checked = response.clearGrecaptcha ?? false;
    elements.serverUrl.value = response.serverUrl || 'http://127.0.0.1:3000';
    elements.operationMode.value = response.operationMode || 'cookie';
    
    updateUIState();
}

// Save settings khi thay đổi (chỉ lưu, không reload)
async function saveSettings() {
    let url = elements.serverUrl.value.trim();
    if (!url) url = 'http://127.0.0.1:3000';
    // Remove trailing slash if exists
    if (url.endsWith('/')) url = url.slice(0, -1);
    // Ensure starts with http
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
        url = 'http://' + url;
    }
    elements.serverUrl.value = url;

    const settings = {
        enabled: elements.enabled.checked,
        flowSessionSyncEnabled: elements.flowSessionSyncEnabled.checked,
        flowCollectorKey: document.getElementById('flowCollectorKey').value,
        autoReload: elements.autoReload.checked,
        reloadInterval: parseInt(elements.reloadInterval.value),
        clearGrecaptcha: elements.clearGrecaptcha.checked,
        serverUrl: url,
        operationMode: elements.operationMode.value
    };
    
    const result = await chrome.runtime.sendMessage({
        type: 'SAVE_SETTINGS',
        settings: settings
    });
    
    if (!result?.success) throw new Error('Settings rejected');
    document.getElementById('flowCollectorKey').value = '';
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
            if (elements.operationMode.value === 'cookie') {
                button.innerHTML = originalHTML;
                button.disabled = false;
                return;
            }
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

const flowMessages = {
    busy: 'Một lượt kiểm tra hoặc đồng bộ đang chạy. Hãy chờ kết quả.',
    captured: 'Phiên Flow hợp lệ; chưa gửi cookie.', synced: 'Máy chủ đã nhận bộ phiên Flow v1.',
    disabled: 'Bật extension, chọn Cookie mode và cho phép đồng bộ Flow rồi lưu.',
    needs_flow_login: 'Mở Flow và đăng nhập đúng một tài khoản.',
    session_changed: 'Phiên thay đổi trong lúc thu; chưa gửi. Hãy kiểm tra lại.',
    receiver_unsupported: 'Máy chủ chưa hỗ trợ Flow v1; chưa gửi cookie.',
    settings_changed: 'Đích nhận đã thay đổi; chưa gửi cookie.',
    rejected: 'Máy chủ không xác nhận nhận phiên.', timeout: 'Hết thời gian kiểm tra hoặc đồng bộ.',
    unavailable: 'Không thể thu bộ phiên hợp lệ. Kiểm tra đăng nhập và quyền extension.',
};
for (const [buttonId, type] of [['checkFlowSession', 'CHECK_FLOW_SESSION'], ['syncFlowSession', 'SYNC_FLOW_SESSION']]) {
    document.getElementById(buttonId).addEventListener('click', async () => {
        const status = document.getElementById('flowSessionStatus');
        try {
            const result = await chrome.runtime.sendMessage({ type });
            status.textContent = flowMessages[result.state] || 'Không thể thực hiện yêu cầu.';
        } catch (_) {
            status.textContent = flowMessages.unavailable;
        }
    });
}
