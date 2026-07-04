/**
 * BananaService — tách biệt logic cũ.
 * Dùng nanoai API (https://flow-api.nanoai.pics) để tạo ảnh / video qua VEO_TOKEN
 * + VEO_COOKIE đã được extension push tới Flask endpoint /api/banana/token-ingest.
 *
 * Flow:
 *   1. Pull token+cookie mới nhất từ Flask: GET /api/banana/token-current?key=admin1103
 *   2. POST /api/v2/images/create | /api/v2/videos/create → nhận taskId
 *   3. Poll GET /api/v2/task?taskId=... cho tới khi status = COMPLETED / FAILED
 *   4. Download imageUrl / videoUrl trả về (HTTP GET trực tiếp)
 *
 * Cách chạy: chạy như Node script độc lập (chưa wire vào Flask worker).
 *   const svc = new BananaService({ nanoToken: "...", flaskBaseUrl: "..." });
 *   await svc.createImage({ promptText: "...", aspectRatio: "IMAGE_ASPECT_RATIO_LANDSCAPE" });
 */

const fs = require("fs");
const path = require("path");

const NANO_BASE = "https://flow-api.nanoai.pics";
const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS_IMAGE = 600 * 1000;   // 600s
const POLL_TIMEOUT_MS_VIDEO = 1200 * 1000;  // 1200s

class BananaService {
    /**
     * @param {object} opts
     * @param {string} opts.nanoToken     - Bearer token để gọi nanoai (NANO_API_KEY)
     * @param {string} opts.flaskBaseUrl  - VD "http://localhost:8000" — server Flask đã ingest VEO token
     * @param {string} [opts.flaskKey]    - admin1103 (default) — query key của ingest endpoint
     * @param {string} [opts.downloadDir] - Thư mục lưu file (default: ./DownloadedNano)
     */
    constructor({ nanoToken, flaskBaseUrl, flaskKey = "admin1103", downloadDir = null } = {}) {
        if (!nanoToken) throw new Error("nanoToken (NANO_API_KEY) is required");
        if (!flaskBaseUrl) throw new Error("flaskBaseUrl is required");
        this.nanoToken = nanoToken;
        this.flaskBaseUrl = flaskBaseUrl.replace(/\/+$/, "");
        this.flaskKey = flaskKey;
        this.downloadDir = downloadDir || path.join(process.cwd(), "DownloadedNano");
        if (!fs.existsSync(this.downloadDir)) fs.mkdirSync(this.downloadDir, { recursive: true });
    }

    // ── Utilities ─────────────────────────────────────────────────────────────
    async _fetchJson(url, opts = {}) {
        const res = await fetch(url, opts);
        const text = await res.text();
        let data;
        try { data = JSON.parse(text); } catch { data = { _raw: text }; }
        if (!res.ok) {
            const err = new Error(`HTTP ${res.status}: ${data?.error || data?._raw || text}`);
            err.status = res.status;
            err.body = data;
            throw err;
        }
        return data;
    }

    /** Pull VEO token+cookie mới nhất từ Flask (extension đã ingest qua /api/banana/token-ingest) */
    async getVeoCredentials() {
        const url = `${this.flaskBaseUrl}/api/banana/token-current?key=${encodeURIComponent(this.flaskKey)}`;
        const data = await this._fetchJson(url, { method: "GET" });
        if (!data.token || !data.cookie) {
            throw new Error("VEO token / cookie chưa được extension ingest. Bật extension và đợi nó push.");
        }
        return { token: data.token, cookie: data.cookie, updatedAt: data.updated_at };
    }

    // ── Create endpoints ──────────────────────────────────────────────────────
    /**
     * Tạo ảnh qua nano. Trả về taskId.
     * @param {object} p
     * @param {string} p.promptText
     * @param {string} [p.aspectRatio]   - IMAGE_ASPECT_RATIO_LANDSCAPE|PORTRAIT
     * @param {string} [p.imageModel]    - default GEM_PIX_2
     * @param {string[]} [p.imageUrls]   - ref images (URL/base64)
     */
    async createImage({ promptText, aspectRatio = "IMAGE_ASPECT_RATIO_LANDSCAPE",
                        imageModel = "GEM_PIX_2", imageUrls = [] }) {
        const { token: accessToken } = await this.getVeoCredentials();
        const body = { accessToken, promptText, imageUrls, aspectRatio, imageModel };
        const data = await this._fetchJson(`${NANO_BASE}/api/v2/images/create`, {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${this.nanoToken}`,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body: JSON.stringify(body),
        });
        const taskId = data?.taskId || data?.id || data?.data?.taskId;
        if (!taskId) throw new Error(`Không có taskId trong response: ${JSON.stringify(data)}`);
        return { taskId, raw: data };
    }

    /**
     * Tạo video qua nano. Trả về taskId.
     * KHÔNG gửi cookie (theo yêu cầu — chỉ accessToken là đủ).
     * @param {object} p
     * @param {string} p.promptText
     * @param {string} [p.aspectRatio]   - VIDEO_ASPECT_RATIO_LANDSCAPE|PORTRAIT|SQUARE
     * @param {string} [p.videoModel]    - default VEO_3_FAST
     * @param {string[]} [p.imageUrls]   - nếu có → I2V (set type='frame')
     * @param {string} [p.type]          - 'frame' khi có imageUrls
     */
    async createVideo({ promptText, aspectRatio = "VIDEO_ASPECT_RATIO_LANDSCAPE",
                        videoModel = "VEO_3_FAST", imageUrls = [], type = null }) {
        const { token: accessToken } = await this.getVeoCredentials();
        // Bỏ cookie khỏi body — chỉ gửi accessToken (đồng nhất với image flow).
        const body = { accessToken, promptText, imageUrls, aspectRatio, videoModel };
        if (type) body.type = type;
        else if (imageUrls.length > 0) body.type = "frame";
        const data = await this._fetchJson(`${NANO_BASE}/api/v2/videos/create`, {
            method: "POST",
            headers: {
                "Authorization": `Bearer ${this.nanoToken}`,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body: JSON.stringify(body),
        });
        const taskId = data?.taskId || data?.id || data?.data?.taskId;
        if (!taskId) throw new Error(`Không có taskId trong response: ${JSON.stringify(data)}`);
        return { taskId, raw: data };
    }

    // ── Poll task ─────────────────────────────────────────────────────────────
    /**
     * Poll task tới khi COMPLETED hoặc FAILED. Timeout 600s ảnh / 1200s video.
     * Trả response cuối cùng (có chứa imageUrl / videoUrl).
     */
    async pollTask(taskId, { isVideo = false, intervalMs = POLL_INTERVAL_MS } = {}) {
        const timeoutMs = isVideo ? POLL_TIMEOUT_MS_VIDEO : POLL_TIMEOUT_MS_IMAGE;
        const startedAt = Date.now();
        const url = `${NANO_BASE}/api/v2/task?taskId=${encodeURIComponent(taskId)}`;
        while (true) {
            if (Date.now() - startedAt > timeoutMs) {
                throw new Error(`Poll timeout sau ${timeoutMs / 1000}s — taskId=${taskId}`);
            }
            let data;
            try {
                data = await this._fetchJson(url, {
                    method: "GET",
                    headers: { "Authorization": `Bearer ${this.nanoToken}`, "Accept": "application/json" },
                });
            } catch (e) {
                console.warn(`[Banana] Poll error (sẽ retry): ${e.message}`);
                await new Promise(r => setTimeout(r, intervalMs));
                continue;
            }
            const status = (data?.status || data?.data?.status || "").toUpperCase();
            if (status === "COMPLETED") return data;
            if (status === "FAILED") {
                throw new Error(`Task FAILED: ${JSON.stringify(data)}`);
            }
            await new Promise(r => setTimeout(r, intervalMs));
        }
    }

    // ── Download ──────────────────────────────────────────────────────────────
    /** Download URL → file. Trả filePath. */
    async downloadFile(url, filename) {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`Download HTTP ${res.status} cho ${url}`);
        const buf = Buffer.from(await res.arrayBuffer());
        const filePath = path.join(this.downloadDir, filename);
        fs.writeFileSync(filePath, buf);
        return filePath;
    }

    // ── High-level helpers ────────────────────────────────────────────────────
    /** Full flow tạo ảnh: createImage → poll → download. */
    async generateImage(opts) {
        const { taskId } = await this.createImage(opts);
        console.log(`[Banana] Image taskId=${taskId} → polling...`);
        const result = await this.pollTask(taskId, { isVideo: false });
        const imageUrl = result?.imageUrl || result?.imageUrls?.[0] || result?.data?.imageUrl;
        if (!imageUrl) throw new Error(`Không có imageUrl trong kết quả: ${JSON.stringify(result)}`);
        const filename = `nano_img_${taskId}.jpg`;
        const filePath = await this.downloadFile(imageUrl, filename);
        console.log(`[Banana] ✅ Saved image → ${filePath}`);
        return { taskId, filePath, raw: result };
    }

    /** Full flow tạo video: createVideo → poll → download. */
    async generateVideo(opts) {
        const { taskId } = await this.createVideo(opts);
        console.log(`[Banana] Video taskId=${taskId} → polling...`);
        const result = await this.pollTask(taskId, { isVideo: true });
        const videoUrl = result?.videoUrl || result?.data?.videoUrl;
        if (!videoUrl) throw new Error(`Không có videoUrl trong kết quả: ${JSON.stringify(result)}`);
        const filename = `nano_vid_${taskId}.mp4`;
        const filePath = await this.downloadFile(videoUrl, filename);
        console.log(`[Banana] ✅ Saved video → ${filePath}`);
        return { taskId, filePath, raw: result };
    }
}

module.exports = BananaService;

// ──────────────────────────────────────────────────────────────────────────────
// CLI test (chạy: node services/BananaService.js)
// ──────────────────────────────────────────────────────────────────────────────
if (require.main === module) {
    (async () => {
        const nanoToken = process.env.NANO_API_KEY;
        const flaskBaseUrl = process.env.FLASK_BASE_URL || "http://localhost:8000";
        if (!nanoToken) {
            console.error("❌ Thiếu env NANO_API_KEY");
            process.exit(1);
        }
        const svc = new BananaService({ nanoToken, flaskBaseUrl });
        const out = await svc.generateImage({
            promptText: "a cute cat in pixar style",
            aspectRatio: "IMAGE_ASPECT_RATIO_LANDSCAPE",
        });
        console.log("Result:", out);
    })().catch(err => {
        console.error("ERROR:", err);
        process.exit(1);
    });
}
