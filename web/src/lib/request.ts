import axios, {AxiosError, type AxiosRequestConfig} from "axios";

import webConfig from "@/constants/common-env";
import {clearStoredAuthSession, getStoredAuthKey} from "@/store/auth";

type RequestConfig = AxiosRequestConfig & {
    redirectOnUnauthorized?: boolean;
};

type ErrorPayload = {
    detail?: string | { error?: string | { message?: string } };
    error?: string | { message?: string };
    message?: string;
};

function errorMessageFromValue(value: unknown): string {
    if (typeof value === "string") {
        return value;
    }
    if (!value || typeof value !== "object") {
        return "";
    }

    const item = value as { error?: unknown; message?: unknown };
    if (typeof item.message === "string") {
        return item.message;
    }
    return errorMessageFromValue(item.error);
}

export const request = axios.create({
    baseURL: webConfig.apiUrl.replace(/\/$/, ""),
});

request.interceptors.request.use(async (config) => {
    const nextConfig = {...config};
    const authKey = await getStoredAuthKey();
    const headers = {...(nextConfig.headers || {})} as Record<string, string>;
    if (authKey && !headers.Authorization) {
        headers.Authorization = `Bearer ${authKey}`;
    }
    // eslint-disable-next-line @typescript-eslint/ban-ts-comment
    // @ts-expect-error
    nextConfig.headers = headers;
    return nextConfig;
});

request.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<ErrorPayload>) => {
        const status = error.response?.status;
        const shouldRedirect = (error.config as RequestConfig | undefined)?.redirectOnUnauthorized !== false;
        if (status === 401 && shouldRedirect && typeof window !== "undefined") {
            // Avoid redirect loop — only redirect if not already on /login
            if (!window.location.pathname.startsWith("/login")) {
                await clearStoredAuthSession();
                window.location.replace("/login");
                // Return a never-resolving promise to prevent further error handling
                // while the browser navigates away
                return new Promise(() => {});
            }
        }

        const payload = error.response?.data;
        const message =
            errorMessageFromValue(payload?.detail) ||
            errorMessageFromValue(payload?.error) ||
            payload?.message ||
            error.message ||
            `请求失败 (${status || 500})`;
        return Promise.reject(new Error(message));
    },
);

type RequestOptions = {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
    redirectOnUnauthorized?: boolean;
};

export async function httpRequest<T>(path: string, options: RequestOptions = {}) {
    const {method = "GET", body, headers, redirectOnUnauthorized = true} = options;
    const config: RequestConfig = {
        url: path,
        method,
        data: body,
        headers,
        redirectOnUnauthorized,
    };
    const response = await request.request<T>(config);
    return response.data;
}

export type StreamRequestOptions = {
    method?: string;
    body?: unknown;
    headers?: Record<string, string>;
    redirectOnUnauthorized?: boolean;
    signal?: AbortSignal;
};

function streamRequestUrl(path: string) {
    if (/^https?:\/\//i.test(path)) {
        return path;
    }
    const baseUrl = webConfig.apiUrl.replace(/\/$/, "");
    if (!baseUrl) {
        return path;
    }
    return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

async function streamResponseErrorMessage(response: Response) {
    const text = await response.text();
    let payload: ErrorPayload | undefined;
    if (text) {
        try {
            payload = JSON.parse(text) as ErrorPayload;
        } catch {
            // Keep the plain response body as a fallback below.
        }
    }
    return (
        errorMessageFromValue(payload?.detail) ||
        errorMessageFromValue(payload?.error) ||
        payload?.message ||
        text ||
        `请求失败 (${response.status || 500})`
    );
}

/**
 * Fetch a long-lived response while sharing the axios client's auth and 401
 * behavior. Existing axios callers should continue using httpRequest/request.
 */
export async function streamRequest(path: string, options: StreamRequestOptions = {}) {
    const {
        method = "GET",
        body,
        headers,
        redirectOnUnauthorized = true,
        signal,
    } = options;
    const authKey = await getStoredAuthKey();
    const nextHeaders: Record<string, string> = {...(headers || {})};
    if (authKey && !Object.keys(nextHeaders).some((key) => key.toLowerCase() === "authorization")) {
        nextHeaders.Authorization = `Bearer ${authKey}`;
    }
    if (body !== undefined && !Object.keys(nextHeaders).some((key) => key.toLowerCase() === "content-type")) {
        nextHeaders["Content-Type"] = "application/json";
    }

    const response = await fetch(streamRequestUrl(path), {
        method,
        headers: nextHeaders,
        body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body),
        signal,
    });

    if (response.status === 401 && redirectOnUnauthorized && typeof window !== "undefined") {
        if (!window.location.pathname.startsWith("/login")) {
            await clearStoredAuthSession();
            window.location.replace("/login");
            return new Promise<Response>(() => {});
        }
    }

    if (!response.ok) {
        throw new Error(await streamResponseErrorMessage(response));
    }
    return response;
}
