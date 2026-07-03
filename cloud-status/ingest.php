<?php
/**
 * ingest.php — receives the weather controller's status snapshot and stores it
 * for the read-only dashboard (index.html). This endpoint ONLY accepts a status
 * blob and writes it to status.json — it has no control capability whatsoever.
 *
 * Auth: a shared token, sent by the controller in the "X-Status-Token" header
 * (with an "Authorization: Bearer" fallback). Set the matching token in
 * secret.php (copy secret.sample.php → secret.php).
 *
 * Deploy: drop this folder on your cPanel/PHP host. The controller's
 * status_push.url points at .../ingest.php; status_push.token matches the token.
 */
declare(strict_types=1);
header('Content-Type: application/json');

// --- load the shared token (kept in a separate, non-output PHP file) --------
$INGEST_TOKEN = '';
$secret = __DIR__ . '/secret.php';
if (is_file($secret)) { require $secret; }

if (!is_string($INGEST_TOKEN) || $INGEST_TOKEN === '') {
    http_response_code(500);
    echo json_encode(['error' => 'server token not configured (create secret.php)']);
    exit;
}

// --- only POST ---------------------------------------------------------------
if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'POST only']);
    exit;
}

// --- check the token (X-Status-Token preferred; Bearer fallback) -------------
$got = $_SERVER['HTTP_X_STATUS_TOKEN'] ?? '';
if ($got === '' && isset($_SERVER['HTTP_AUTHORIZATION'])) {
    if (preg_match('/Bearer\s+(.+)/i', $_SERVER['HTTP_AUTHORIZATION'], $m)) {
        $got = trim($m[1]);
    }
}
if (!hash_equals($INGEST_TOKEN, (string) $got)) {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

// --- read + validate the body (cap size; must be a JSON object) --------------
// Read one byte past the cap so an oversized snapshot is REJECTED (413) rather
// than silently truncated into invalid JSON that leaves the mirror stale.
$MAX = 262144; // 256 KB
$raw = file_get_contents('php://input', false, null, 0, $MAX + 1);
if (strlen((string) $raw) > $MAX) {
    http_response_code(413);
    echo json_encode(['error' => 'status payload too large']);
    exit;
}
$data = json_decode((string) $raw, true);
if (!is_array($data)) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid json']);
    exit;
}

// --- store atomically next to this script ------------------------------------
$tmp = __DIR__ . '/status.json.tmp';
$dst = __DIR__ . '/status.json';
if (file_put_contents($tmp, $raw, LOCK_EX) === false || !rename($tmp, $dst)) {
    http_response_code(500);
    echo json_encode(['error' => 'could not store status']);
    exit;
}

echo json_encode(['ok' => true]);
