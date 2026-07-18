document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".delete-form").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (!window.confirm("确定删除此文件？此操作无法撤销。")) {
                event.preventDefault();
            }
        });
    });

    const form = document.querySelector("#upload-form");
    if (!(form instanceof HTMLFormElement)) {
        return;
    }

    const fileInput = form.querySelector('input[type="file"]');
    const button = form.querySelector("[data-upload-button]");
    const progressBox = form.querySelector("[data-upload-progress]");
    const progress = progressBox?.querySelector("progress");
    const status = progressBox?.querySelector("[data-upload-status]");
    const maxFiles = Number(form.dataset.maxFiles || "0");
    const maxFileBytes = Number(form.dataset.maxFileBytes || "0");

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!(fileInput instanceof HTMLInputElement) || !fileInput.files?.length) {
            return;
        }

        const files = Array.from(fileInput.files);
        if (maxFiles > 0 && files.length > maxFiles) {
            window.alert(`单次最多上传 ${maxFiles} 个文件。`);
            return;
        }

        const oversized = files.find((file) => maxFileBytes > 0 && file.size > maxFileBytes);
        if (oversized) {
            window.alert(`文件“${oversized.name}”超过单文件大小限制。`);
            return;
        }

        if (button instanceof HTMLButtonElement) {
            button.disabled = true;
            button.textContent = "上传中…";
        }
        if (progressBox instanceof HTMLElement) {
            progressBox.hidden = false;
        }
        if (status instanceof HTMLElement) {
            status.textContent = "正在上传 0%";
        }

        const request = new XMLHttpRequest();
        request.open("POST", form.action);
        request.upload.addEventListener("progress", (progressEvent) => {
            if (!progressEvent.lengthComputable) {
                if (status instanceof HTMLElement) {
                    status.textContent = "正在上传…";
                }
                return;
            }
            const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
            if (progress instanceof HTMLProgressElement) {
                progress.value = percent;
            }
            if (status instanceof HTMLElement) {
                status.textContent = `正在上传 ${percent}%`;
            }
        });
        request.addEventListener("load", () => {
            if (request.status >= 200 && request.status < 400) {
                window.location.assign(form.dataset.redirect || "/admin");
                return;
            }
            restoreButton(`上传失败：${request.responseText || request.status}`);
        });
        request.addEventListener("error", () => {
            restoreButton("网络错误，上传未完成。请重试。");
        });
        request.send(new FormData(form));
    });

    function restoreButton(message) {
        if (button instanceof HTMLButtonElement) {
            button.disabled = false;
            button.textContent = "上传文件";
        }
        if (status instanceof HTMLElement) {
            status.textContent = message;
        }
    }
});
