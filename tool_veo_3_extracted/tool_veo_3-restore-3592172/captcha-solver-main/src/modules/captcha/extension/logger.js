// ===================================
// LOGGER UTILITY - NestJS style logging
// ===================================

class Logger {
    constructor(context) {
        this.context = context;
    }

    log(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.log(`${prefix} ${message}`, data);
        } else {
            console.log(`${prefix} ${message}`);
        }
    }

    debug(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.debug(`${prefix} ${message}`, data);
        } else {
            console.debug(`${prefix} ${message}`);
        }
    }

    warn(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.warn(`${prefix} ⚠️ ${message}`, data);
        } else {
            console.warn(`${prefix} ⚠️ ${message}`);
        }
    }

    error(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.error(`${prefix} ❌ ${message}`, data);
        } else {
            console.error(`${prefix} ❌ ${message}`);
        }
    }

    success(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.log(`${prefix} ✅ ${message}`, data);
        } else {
            console.log(`${prefix} ✅ ${message}`);
        }
    }

    info(message, data = null) {
        const prefix = `[${this.context}]`;
        if (data) {
            console.log(`${prefix} ℹ️ ${message}`, data);
        } else {
            console.log(`${prefix} ℹ️ ${message}`);
        }
    }
}

// Export for use in content.js and popup.js
if (typeof module !== 'undefined' && module.exports) {
    module.exports = Logger;
}
