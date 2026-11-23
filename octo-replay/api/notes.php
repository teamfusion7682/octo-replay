<?php
declare(strict_types=1);

/**
 * Octo-Replay · Topluluk scouting notları API'si (PDO + hazır ifadeler = SQL Injection korumalı).
 *
 * GET  api/notes.php?match_key=2026tuis_qm12[&team=7682][&limit=50][&before_id=120]
 * POST api/notes.php   (JSON gövde; "X-Requested-With: OctoReplay" başlığı zorunlu)
 *      {"match_key":"2026tuis_qm12","team_number":7682|null,"author_team":254,"author_name":"Ali",
 *       "category":"general|auto|cycle|defense|reliability|endgame","rating":1-5|null,"note":"...","website":""}
 *
 * "website" alanı bot tuzağıdır (honeypot); dolu gelirse istek sessizce yok sayılır.
 */

require __DIR__ . '/lib.php';

const NOTES_MATCH_KEY_RE = '/^\d{4}[a-z0-9]{2,20}_[a-z0-9]{2,16}$/';
const NOTES_CATEGORIES   = ['general', 'auto', 'cycle', 'defense', 'reliability', 'endgame'];
const NOTES_MAX_BODY     = 16384;

/** Denetim karakterlerini ve görünmez biçim karakterlerini temizler (satır sonu korunur). */
function notes_clean(string $text, bool $allowNewlines): string
{
    $clean = preg_replace('/[^\P{C}\n]+/u', '', str_replace(["\r\n", "\r"], "\n", $text));
    if ($clean === null) {
        throw new OctoHttpException('Metin geçerli UTF-8 değil.', 400);
    }
    if (!$allowNewlines) {
        $clean = preg_replace('/\s+/u', ' ', $clean) ?? '';
    } else {
        $clean = preg_replace('/\n{3,}/', "\n\n", $clean) ?? '';
    }
    return trim($clean);
}

function notes_iso_utc(string $value): string
{
    $dt = DateTimeImmutable::createFromFormat('Y-m-d H:i:s', $value, new DateTimeZone('UTC'));
    return $dt instanceof DateTimeImmutable ? $dt->format('Y-m-d\TH:i:s\Z') : $value;
}

function notes_format_row(array $row): array
{
    return [
        'id'          => (int)$row['id'],
        'match_key'   => (string)$row['match_key'],
        'team_number' => $row['team_number'] === null ? null : (int)$row['team_number'],
        'author_team' => (int)$row['author_team'],
        'author_name' => (string)$row['author_name'],
        'category'    => (string)$row['category'],
        'rating'      => $row['rating'] === null ? null : (int)$row['rating'],
        'note'        => (string)$row['note'],
        'created_at'  => notes_iso_utc((string)$row['created_at']),
    ];
}

function notes_bind_nullable_int(PDOStatement $stmt, string $name, ?int $value): void
{
    $stmt->bindValue($name, $value, $value === null ? PDO::PARAM_NULL : PDO::PARAM_INT);
}

function notes_list(): void
{
    $cfg      = octo_config();
    $pageSize = max(1, (int)($cfg['notes']['page_size'] ?? 100));
    $matchKey = (string)octo_param('match_key', NOTES_MATCH_KEY_RE);
    $team     = octo_param('team', '/^\d{1,5}$/', false);
    $before   = octo_param('before_id', '/^\d{1,10}$/', false);
    $limit    = (int)octo_param('limit', '/^\d{1,3}$/', false, (string)$pageSize);
    $limit    = max(1, min($pageSize, $limit));

    $sql = 'SELECT id, match_key, team_number, author_team, author_name, category, rating, note, created_at
            FROM scouting_notes
            WHERE is_hidden = 0 AND match_key = :match_key';
    if ($team !== null) {
        $sql .= ' AND team_number = :team';
    }
    if ($before !== null) {
        $sql .= ' AND id < :before';
    }
    $sql .= ' ORDER BY id DESC LIMIT :limit';

    $stmt = octo_pdo()->prepare($sql);
    $stmt->bindValue(':match_key', $matchKey, PDO::PARAM_STR);
    if ($team !== null) {
        $stmt->bindValue(':team', (int)$team, PDO::PARAM_INT);
    }
    if ($before !== null) {
        $stmt->bindValue(':before', (int)$before, PDO::PARAM_INT);
    }
    $stmt->bindValue(':limit', $limit, PDO::PARAM_INT);
    $stmt->execute();

    $notes = array_map('notes_format_row', $stmt->fetchAll());
    octo_respond(['ok' => true, 'notes' => $notes, 'count' => count($notes)]);
}

function notes_assert_same_origin(): void
{
    $requestedWith = $_SERVER['HTTP_X_REQUESTED_WITH'] ?? '';
    if ($requestedWith !== 'OctoReplay') {
        throw new OctoHttpException('Geçersiz istek kaynağı.', 403);
    }
    $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
    if ($origin !== '') {
        $originHost = parse_url($origin, PHP_URL_HOST);
        $originPort = parse_url($origin, PHP_URL_PORT);
        $host       = (string)($_SERVER['HTTP_HOST'] ?? '');
        $expected   = is_string($originHost) ? $originHost . ($originPort ? ':' . $originPort : '') : '';
        if ($expected === '' || strcasecmp($expected, $host) !== 0) {
            throw new OctoHttpException('Geçersiz istek kaynağı.', 403);
        }
    }
}

/** @return array<string,mixed> */
function notes_read_json_body(): array
{
    $contentType = strtolower((string)($_SERVER['CONTENT_TYPE'] ?? ''));
    if (!str_contains($contentType, 'application/json')) {
        throw new OctoHttpException("Content-Type 'application/json' olmalıdır.", 415);
    }
    $raw = file_get_contents('php://input', false, null, 0, NOTES_MAX_BODY + 1);
    if ($raw === false || $raw === '') {
        throw new OctoHttpException('İstek gövdesi boş.', 400);
    }
    if (strlen($raw) > NOTES_MAX_BODY) {
        throw new OctoHttpException('İstek gövdesi çok büyük.', 413);
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || json_last_error() !== JSON_ERROR_NONE) {
        throw new OctoHttpException('Geçersiz JSON.', 400);
    }
    return $data;
}

function notes_int_field(array $data, string $field, int $min, int $max, bool $required): ?int
{
    $value = $data[$field] ?? null;
    if ($value === null || $value === '') {
        if ($required) {
            throw new OctoHttpException("'{$field}' alanı gerekli.", 422);
        }
        return null;
    }
    if (is_string($value) && preg_match('/^\d{1,6}$/', $value) === 1) {
        $value = (int)$value;
    }
    if (!is_int($value) || $value < $min || $value > $max) {
        throw new OctoHttpException("'{$field}' alanı {$min}-{$max} arasında bir sayı olmalıdır.", 422);
    }
    return $value;
}

function notes_create(): void
{
    notes_assert_same_origin();
    $cfg  = octo_config();
    $data = notes_read_json_body();

    // Honeypot: botlar görünmeyen alanı doldurur. Başarılı gibi yanıtla, kaydetme.
    if (isset($data['website']) && trim((string)$data['website']) !== '') {
        octo_respond(['ok' => true, 'ignored' => true], 202);
    }

    $matchKey = strtolower(trim((string)($data['match_key'] ?? '')));
    if (preg_match(NOTES_MATCH_KEY_RE, $matchKey) !== 1) {
        throw new OctoHttpException("'match_key' geçersiz.", 422);
    }
    $eventKey = explode('_', $matchKey, 2)[0];

    $teamNumber = notes_int_field($data, 'team_number', 1, 65535, false);
    $authorTeam = notes_int_field($data, 'author_team', 1, 65535, true);
    $rating     = notes_int_field($data, 'rating', 1, 5, false);

    $category = (string)($data['category'] ?? 'general');
    if (!in_array($category, NOTES_CATEGORIES, true)) {
        throw new OctoHttpException("'category' geçersiz.", 422);
    }

    $authorName = octo_substr(notes_clean((string)($data['author_name'] ?? ''), false), 0, 60);

    $maxLength = max(10, (int)($cfg['notes']['max_length'] ?? 1000));
    $note      = notes_clean((string)($data['note'] ?? ''), true);
    $length    = octo_strlen($note);
    if ($length < 3) {
        throw new OctoHttpException('Not en az 3 karakter olmalıdır.', 422);
    }
    if ($length > $maxLength) {
        throw new OctoHttpException("Not en fazla {$maxLength} karakter olabilir.", 422);
    }
    if (preg_match_all('~https?://~i', $note) > 2) {
        throw new OctoHttpException('Notta en fazla 2 bağlantı olabilir.', 422);
    }

    $ip     = (string)($_SERVER['REMOTE_ADDR'] ?? '0.0.0.0');
    $ipHash = hash('sha256', $ip . '|' . (string)($cfg['notes']['ip_salt'] ?? ''));

    $pdo    = octo_pdo();
    $window = max(30, (int)($cfg['notes']['rate_limit_window'] ?? 600));
    $limit  = max(1, (int)($cfg['notes']['rate_limit_count'] ?? 8));
    $since  = gmdate('Y-m-d H:i:s', time() - $window);

    $stmt = $pdo->prepare('SELECT COUNT(*) FROM scouting_notes WHERE ip_hash = :ip AND created_at >= :since');
    $stmt->execute([':ip' => $ipHash, ':since' => $since]);
    if ((int)$stmt->fetchColumn() >= $limit) {
        octo_fail('Çok sık not gönderiyorsunuz. Lütfen birkaç dakika sonra tekrar deneyin.', 429, [], ['Retry-After' => (string)$window]);
    }

    $stmt = $pdo->prepare(
        'SELECT id FROM scouting_notes
         WHERE ip_hash = :ip AND match_key = :match_key AND note = :note AND created_at >= :since LIMIT 1'
    );
    $stmt->execute([':ip' => $ipHash, ':match_key' => $matchKey, ':note' => $note, ':since' => $since]);
    if ($stmt->fetchColumn() !== false) {
        octo_fail('Bu not zaten gönderilmiş.', 409);
    }

    $insert = $pdo->prepare(
        'INSERT INTO scouting_notes
            (match_key, event_key, team_number, author_team, author_name, category, rating, note, ip_hash)
         VALUES
            (:match_key, :event_key, :team_number, :author_team, :author_name, :category, :rating, :note, :ip_hash)'
    );
    $insert->bindValue(':match_key', $matchKey, PDO::PARAM_STR);
    $insert->bindValue(':event_key', $eventKey, PDO::PARAM_STR);
    notes_bind_nullable_int($insert, ':team_number', $teamNumber);
    $insert->bindValue(':author_team', $authorTeam, PDO::PARAM_INT);
    $insert->bindValue(':author_name', $authorName, PDO::PARAM_STR);
    $insert->bindValue(':category', $category, PDO::PARAM_STR);
    notes_bind_nullable_int($insert, ':rating', $rating);
    $insert->bindValue(':note', $note, PDO::PARAM_STR);
    $insert->bindValue(':ip_hash', $ipHash, PDO::PARAM_STR);
    $insert->execute();

    $id  = (int)$pdo->lastInsertId();
    $sel = $pdo->prepare(
        'SELECT id, match_key, team_number, author_team, author_name, category, rating, note, created_at
         FROM scouting_notes WHERE id = :id'
    );
    $sel->execute([':id' => $id]);
    $row = $sel->fetch();

    octo_respond(['ok' => true, 'note' => $row ? notes_format_row($row) : null], 201);
}

octo_send_headers('no-store');

try {
    $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
    if ($method === 'GET') {
        notes_list();
    } elseif ($method === 'POST') {
        notes_create();
    } else {
        octo_fail('Desteklenmeyen yöntem.', 405, [], ['Allow' => 'GET, POST']);
    }
} catch (OctoHttpException $e) {
    octo_fail($e->getMessage(), $e->httpStatus());
} catch (PDOException $e) {
    error_log('[octo-replay] notes PDO hatası: ' . $e->getMessage());
    octo_fail('Veritabanı hatası. Şemanın (schema.sql) yüklendiğinden emin olun.', 500);
} catch (Throwable $e) {
    error_log('[octo-replay] notes beklenmeyen hata: ' . $e->getMessage() . ' @ ' . $e->getFile() . ':' . $e->getLine());
    octo_fail('Sunucu hatası.', 500);
}
