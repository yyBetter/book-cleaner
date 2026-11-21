/**
 * Hands-On-Page Zone-Aware Smart Auto-Capture engine for browser & Android Chrome live cameras.
 * Ignores outer thumb-holding margins (16% left/right), blocks while hand crosses center text,
 * deduplicates same-page jitter via 64-bit dHash, and picks the sharpest frame in the settle burst.
 */

export class HandsOnSmartTrigger {
  constructor(options = {}) {
    this.enabled = Boolean(options.enabled);
    this.handsOnAssist = options.handsOnAssist !== false;
    this.marginRatio = options.marginRatio ?? 0.16;
    this.motionThreshold = options.motionThreshold ?? 10.5;
    this.settleThreshold = options.settleThreshold ?? 3.2;
    this.settleMs = options.settleMs ?? 380;
    this.cooldownMs = options.cooldownMs ?? 1200;
    this.centerSkinMaxRatio = options.centerSkinMaxRatio ?? 0.035;
    this.pageChangeHamming = options.pageChangeHamming ?? 7;

    this.width = 200;
    this.height = 150;
    this.offCanvas = document.createElement("canvas");
    this.offCanvas.width = this.width;
    this.offCanvas.height = this.height;
    this.offCtx = this.offCanvas.getContext("2d", { willReadFrequently: true });

    this.state = "IDLE";
    this.label = "等待翻页（双手可压住左右白边）";
    this.prevRoiGray = null;
    this.settleStartedAt = 0;
    this.cooldownStartedAt = 0;
    this.lastCapturedHash = null;
    this.bestSharpness = -1;
    this.bestDataUrl = null;
    this.triggerCount = 0;
  }

  reset({ clearLastHash = false } = {}) {
    this.state = "IDLE";
    this.label = "等待翻页（双手可压住左右白边）";
    this.prevRoiGray = null;
    this.settleStartedAt = 0;
    this.cooldownStartedAt = 0;
    this.bestSharpness = -1;
    this.bestDataUrl = null;
    if (clearLastHash) this.lastCapturedHash = null;
  }

  resetLastHash() {
    this.lastCapturedHash = null;
  }

  _analyzeVideo(videoEl) {
    this.offCtx.drawImage(videoEl, 0, 0, this.width, this.height);
    const rgba = this.offCtx.getImageData(0, 0, this.width, this.height).data;

    const m = this.handsOnAssist ? Math.max(0, Math.min(0.28, this.marginRatio)) : 0;
    const x0 = Math.floor(this.width * m);
    const x1 = Math.max(x0 + 10, Math.ceil(this.width * (1 - m)));
    const y0 = this.handsOnAssist ? Math.floor(this.height * 0.08) : 0;
    const y1 = this.handsOnAssist ? Math.max(y0 + 10, Math.ceil(this.height * 0.92)) : this.height;

    const roiW = x1 - x0;
    const roiH = y1 - y0;
    const roiGray = new Float32Array(roiW * roiH);

    let skinCount = 0;
    let idx = 0;
    for (let y = y0; y < y1; y += 1) {
      for (let x = x0; x < x1; x += 1) {
        const p = (y * this.width + x) * 4;
        const r = rgba[p];
        const g = rgba[p + 1];
        const b = rgba[p + 2];
        const lum = 0.299 * r + 0.587 * g + 0.114 * b;
        roiGray[idx++] = lum;

        if (this.handsOnAssist) {
          const cr = 128 + 0.713 * (r - lum);
          const cb = 128 + 0.564 * (b - lum);
          const maxC = Math.max(r, g, b);
          const minC = Math.min(r, g, b);
          const sat = maxC > 0 ? ((maxC - minC) / maxC) * 255 : 0;
          // Exclude aged yellowed paper while capturing real skin tones
          if (
            cr >= 139 &&
            cr <= 180 &&
            cb >= 75 &&
            cb <= 128 &&
            lum >= 50 &&
            lum <= 235 &&
            sat >= 28 &&
            sat <= 210 &&
            r > g + 12 &&
            r > b
          ) {
            skinCount += 1;
          }
        }
      }
    }

    const centerSkinRatio = skinCount / Math.max(1, roiGray.length);
    const centerHash = this._computeRoiDhash(roiGray, roiW, roiH);
    const sharpness = this._computeRoiSharpness(roiGray, roiW, roiH);

    let roiMad = 0;
    if (this.prevRoiGray && this.prevRoiGray.length === roiGray.length) {
      let diffSum = 0;
      for (let i = 0; i < roiGray.length; i += 1) {
        diffSum += Math.abs(roiGray[i] - this.prevRoiGray[i]);
      }
      roiMad = diffSum / roiGray.length;
    }
    this.prevRoiGray = roiGray;

    return { roiMad, centerSkinRatio, centerHash, sharpness };
  }

  _computeRoiDhash(roiGray, w, h) {
    // Downsample central ROI to 9x8 using bilinear/point sampling for 64-bit dHash
    const cols = 9;
    const rows = 8;
    const grid = new Float32Array(cols * rows);
    for (let r = 0; r < rows; r += 1) {
      const sy = Math.min(h - 1, Math.floor(((r + 0.5) * h) / rows));
      for (let c = 0; c < cols; c += 1) {
        const sx = Math.min(w - 1, Math.floor(((c + 0.5) * w) / cols));
        grid[r * cols + c] = roiGray[sy * w + sx];
      }
    }
    let bits = 0n;
    for (let r = 0; r < rows; r += 1) {
      for (let c = 0; c < rows; c += 1) {
        bits = (bits << 1n) | (grid[r * cols + c + 1] > grid[r * cols + c] ? 1n : 0n);
      }
    }
    return bits;
  }

  _hamming(a, b) {
    if (a === null || b === null) return 64;
    let x = a ^ b;
    let count = 0;
    while (x > 0n) {
      x &= x - 1n;
      count += 1;
    }
    return count;
  }

  _computeRoiSharpness(roiGray, w, h) {
    let sum = 0;
    let sumSq = 0;
    let n = 0;
    for (let y = 1; y < h - 1; y += 1) {
      for (let x = 1; x < w - 1; x += 1) {
        const i = y * w + x;
        const lap = roiGray[i - 1] + roiGray[i + 1] + roiGray[i - w] + roiGray[i + w] - 4 * roiGray[i];
        sum += lap;
        sumSq += lap * lap;
        n += 1;
      }
    }
    if (n === 0) return 0;
    const mean = sum / n;
    return sumSq / n - mean * mean;
  }

  markManualCapture(videoEl) {
    if (!videoEl || !videoEl.videoWidth) return;
    const { centerHash } = this._analyzeVideo(videoEl);
    this.lastCapturedHash = centerHash;
  }

  step(videoEl, captureSnapshotFn) {
    if (!videoEl || !videoEl.videoWidth) {
      return { state: "IDLE", label: "摄像头尚未就绪", shouldCapture: false, settleProgress: 0 };
    }
    const now = performance.now();
    const { roiMad, centerSkinRatio, centerHash, sharpness } = this._analyzeVideo(videoEl);

    if (!this.enabled) {
      this.state = "IDLE";
      this.label = "手动拍摄模式";
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress: 0,
        roiMad,
        centerSkinRatio,
      };
    }

    if (this.state === "COOLDOWN") {
      if (now - this.cooldownStartedAt >= this.cooldownMs) {
        this.state = "IDLE";
        this.label = "等待翻页（双手可压住左右白边）";
      }
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress: 0,
        roiMad,
        centerSkinRatio,
      };
    }

    if (this.handsOnAssist && centerSkinRatio > this.centerSkinMaxRatio) {
      this.state = "HAND_IN_CENTER";
      this.label = "手在正文区，撤回边缘后自动抓拍";
      this.bestSharpness = -1;
      this.bestDataUrl = null;
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress: 0,
        roiMad,
        centerSkinRatio,
      };
    }

    const isNewContent =
      this.lastCapturedHash === null || this._hamming(centerHash, this.lastCapturedHash) >= this.pageChangeHamming;

    if (this.state === "IDLE") {
      if (roiMad >= this.motionThreshold) {
        this.state = "MOTION_DETECTED";
        this.label = "检测到翻页动作...";
      } else if (this.lastCapturedHash !== null && isNewContent && roiMad <= this.settleThreshold) {
        this.state = "SETTLING";
        this.label = "画面稳住，优选最清晰帧...";
        this.settleStartedAt = now;
        this.bestSharpness = sharpness;
        this.bestDataUrl = captureSnapshotFn ? captureSnapshotFn() : null;
      }
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress: 0,
        roiMad,
        centerSkinRatio,
      };
    }

    if (this.state === "MOTION_DETECTED" || this.state === "HAND_IN_CENTER") {
      if (roiMad <= this.settleThreshold) {
        this.state = "SETTLING";
        this.label = "画面稳住，优选最清晰帧...";
        this.settleStartedAt = now;
        this.bestSharpness = sharpness;
        this.bestDataUrl = captureSnapshotFn ? captureSnapshotFn() : null;
      } else {
        this.state = "MOTION_DETECTED";
        this.label = "检测到翻页动作...";
      }
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress: 0,
        roiMad,
        centerSkinRatio,
      };
    }

    if (this.state === "SETTLING") {
      if (roiMad >= this.motionThreshold) {
        this.state = "MOTION_DETECTED";
        this.label = "检测到翻页动作...";
        this.bestSharpness = -1;
        this.bestDataUrl = null;
        return {
          state: this.state,
          label: this.label,
          shouldCapture: false,
          settleProgress: 0,
          roiMad,
          centerSkinRatio,
        };
      }

      if (sharpness >= this.bestSharpness) {
        this.bestSharpness = sharpness;
        if (captureSnapshotFn) this.bestDataUrl = captureSnapshotFn();
      }

      const elapsed = now - this.settleStartedAt;
      const settleProgress = Math.min(1, Math.max(0, elapsed / Math.max(1, this.settleMs)));
      if (elapsed >= this.settleMs) {
        if (this.lastCapturedHash !== null && !isNewContent) {
          this.state = "IDLE";
          this.label = "页内容未变，已跳过重复拍摄";
          this.bestSharpness = -1;
          this.bestDataUrl = null;
          return {
            state: this.state,
            label: this.label,
            shouldCapture: false,
            settleProgress: 0,
            roiMad,
            centerSkinRatio,
          };
        }

        const bestSnapshot = this.bestDataUrl;
        this.bestSharpness = -1;
        this.bestDataUrl = null;
        this.lastCapturedHash = centerHash;
        this.state = "COOLDOWN";
        this.label = "已抓拍入库，请翻下一页";
        this.cooldownStartedAt = now;
        this.triggerCount += 1;
        return {
          state: this.state,
          label: this.label,
          shouldCapture: true,
          bestSnapshot,
          settleProgress: 1,
          roiMad,
          centerSkinRatio,
        };
      }
      return {
        state: this.state,
        label: this.label,
        shouldCapture: false,
        settleProgress,
        roiMad,
        centerSkinRatio,
      };
    }

    return { state: this.state, label: this.label, shouldCapture: false, settleProgress: 0, roiMad, centerSkinRatio };
  }
}
