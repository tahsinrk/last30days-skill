/**
 * Twitter cookie/credential resolution from environment variables and CLI args.
 * Browser extraction has been removed — credentials must be provided explicitly.
 */
function normalizeValue(value) {
    if (typeof value !== 'string') {
        return null;
    }
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
}
function cookieHeader(authToken, ct0) {
    return `auth_token=${authToken}; ct0=${ct0}`;
}
export function buildEmpty() {
    return { authToken: null, ct0: null, cookieHeader: null, source: null };
}
export function readEnvCookie(cookies, keys, field) {
    if (cookies[field]) {
        return;
    }
    for (const key of keys) {
        const value = normalizeValue(process.env[key]);
        if (!value) {
            continue;
        }
        cookies[field] = value;
        if (!cookies.source) {
            cookies.source = `env ${key}`;
        }
        break;
    }
}
/**
 * Resolve Twitter credentials from CLI args and environment variables.
 * Priority: CLI args > environment variables.
 */
export async function resolveCredentials(options) {
    const warnings = [];
    const cookies = buildEmpty();
    if (options.authToken) {
        cookies.authToken = options.authToken;
        cookies.source = 'CLI argument';
    }
    if (options.ct0) {
        cookies.ct0 = options.ct0;
        if (!cookies.source) {
            cookies.source = 'CLI argument';
        }
    }
    readEnvCookie(cookies, ['AUTH_TOKEN', 'TWITTER_AUTH_TOKEN'], 'authToken');
    readEnvCookie(cookies, ['CT0', 'TWITTER_CT0'], 'ct0');
    if (!cookies.authToken) {
        warnings.push('Missing auth_token - provide via --auth-token or AUTH_TOKEN env var');
    }
    if (!cookies.ct0) {
        warnings.push('Missing ct0 - provide via --ct0 or CT0 env var');
    }
    if (cookies.authToken && cookies.ct0) {
        cookies.cookieHeader = cookieHeader(cookies.authToken, cookies.ct0);
    }
    return { cookies, warnings };
}
export { normalizeValue, cookieHeader };
//# sourceMappingURL=cookies.js.map
