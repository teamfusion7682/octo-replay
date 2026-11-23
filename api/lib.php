<?php
declare(strict_types=1);

/**
 * Octo-Replay · ortak yardımcılar (JSON yanıtı, doğrulama, PDO bağlantısı).
 * PHP 8.0+ ile uyumludur.
 */

final class OctoHttpException extends RuntimeException
{
    private int $httpStatus;

    public function __construct(string $message, int $httpStatus = 500, ?Throwable $previous = null)
    {
        parent::__construct($message, 0, $previous);
        $this->httpStatus = $httpStatus;
    }

    public function httpStatus(): int
    {
        return $this->httpStatus;
    }
}

function octo_config(): array
{
    static $cfg = null;
    if ($cfg === null) {
        $loaded = require __DIR__ . '/config.php';
        if (!is_array($loaded)) {
            throw new RuntimeException('config.php bir dizi döndürmelidir.');
        }
        $cfg = $loaded;
    }
    return $cfg;
}

function octo_lower(string $value): string
{
    return function_exists('mb_strtolower') ? mb_strtolower($value, 'UTF-8') : strtolower($value);
}

function octo_strlen(string $value): int
{
    return function_exists('mb_strlen') ? mb_strlen($value, 'UTF-8') : strlen($value);
}

function octo_substr(string $value, int $start, int $length): string
{
    return function_exists('mb_substr') ? mb_substr($value, $start, $length, 'UTF-8') : substr($value, $start, $length);
}

/** Güvenlik ve içerik türü başlıkları. */
function octo_send_headers(string $cacheControl = 'no-store'): void
{
    if (headers_sent()) {
        return;
    }
    header('Content-Type: application/json; charset=utf-8');
    header('X-Content-Type-Options: nosniff');
    header('Referrer-Policy: same-origin');
    header('Cache-Control: ' . $cacheControl);
}

/** JSON yanıtını yazar ve betiği sonlandırır. */
function octo_respond(array $payload, int $status = 200, array $headers = []): void
{
    if (!headers_sent()) {
        http_response_code($status);
        foreach ($headers as $name => $value) {
            header($name . ': ' . $value);
        }
    }
    $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR;
    $json = json_encode($payload, $flags);
    if ($json === false) {
        if (!headers_sent()) {
            http_response_code(500);
        }
        $json = '{"ok":false,"error":"Yanıt JSON olarak üretilemedi."}';
    }
    echo $json;
    exit;
}

function octo_fail(string $message, int $status = 400, array $extra = [], array $headers = []): void
{
    octo_respond(['ok' => false, 'error' => $message] + $extra, $status, $headers);
}

/**
 * GET parametresini regex ile doğrular. Zorunlu parametre eksik/geçersizse 400 fırlatır.
 */
function octo_param(string $name, string $pattern, bool $required = true, ?string $default = null): ?string
{
    $raw = $_GET[$name] ?? null;
    if ($raw === null || $raw === '') {
        if ($required && $default === null) {
            throw new OctoHttpException("'{$name}' parametresi gerekli.", 400);
        }
        return $default;
    }
    if (!is_string($raw)) {
        throw new OctoHttpException("'{$name}' parametresi geçersiz.", 400);
    }
    $value = trim($raw);
    if (strlen($value) > 64 || preg_match($pattern, $value) !== 1) {
        throw new OctoHttpException("'{$name}' parametresi geçersiz.", 400);
    }
    return $value;
}

function octo_pdo(): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }

    $db  = octo_config()['db'] ?? [];
    $dsn = $db['dsn'] ?? sprintf(
        'mysql:host=%s;dbname=%s;charset=%s',
        $db['host'] ?? 'localhost',
        $db['name'] ?? '',
        $db['charset'] ?? 'utf8mb4'
    );

    try {
        $pdo = new PDO($dsn, $db['user'] ?? null, $db['pass'] ?? null, [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES   => false,
            PDO::ATTR_TIMEOUT            => 5,
        ]);
    } catch (PDOException $e) {
        error_log('[octo-replay] Veritabanı bağlantı hatası: ' . $e->getMessage());
        throw new OctoHttpException('Veritabanına bağlanılamadı.', 503, $e);
    }

    if ($pdo->getAttribute(PDO::ATTR_DRIVER_NAME) === 'mysql') {
        $pdo->exec("SET time_zone = '+00:00'");
    }
    return $pdo;
}
