<?php
declare(strict_types=1);

/**
 * Octo-Replay · The Blue Alliance (TBA) API v3 vekil katmanı.
 *
 * - API anahtarı yalnızca sunucuda kalır; tarayıcıya hiç gönderilmez.
 * - Yanıtlar `cache/` klasöründe 10 dakika (config: cache_ttl) dosya bazlı önbelleğe alınır.
 * - TBA erişilemezse süresi dolmuş önbellek (stale_ttl içinde) "stale" işaretiyle sunulur.
 *
 * Uç noktalar (yalnızca GET):
 *   ?action=events&year=2026
 *   ?action=event&event=2026tuis
 *   ?action=teams&event=2026tuis
 *   ?action=rankings&event=2026tuis
 *   ?action=matches&event=2026tuis
 *   ?action=match&match=2026tuis_qm12
 *   ?action=team_matches&team=7682&year=2026
 *
 * Başarılı yanıt : {"ok":true,"data":...,"meta":{"cached":bool,"stale":bool,"fetched_at":int,"ttl":int}}
 * Hata yanıtı    : {"ok":false,"error":"..."}  (uygun HTTP durum koduyla)
 */

require __DIR__ . '/lib.php';

const TBA_EVENT_KEY_RE = '/^\d{4}[a-z0-9]{2,20}$/';
const TBA_MATCH_KEY_RE = '/^\d{4}[a-z0-9]{2,20}_[a-z0-9]{2,16}$/';

// ---------------------------------------------------------------------------
// Önbellek
// ---------------------------------------------------------------------------

function tba_cache_path(string $dir, string $key): string
{
    return rtrim($dir, '/\\') . '/tba_' . sha1($key) . '.json';
}

function tba_cache_read(string $file): ?array
{
    if (!is_file($file) || !is_readable($file)) {
        return null;
    }
    $raw = @file_get_contents($file);
    if ($raw === false || $raw === '') {
        return null;
    }
    $decoded = json_decode($raw, true);
    if (!is_array($decoded) || !isset($decoded['fetched_at']) || !array_key_exists('data', $decoded)) {
        return null;
    }
    return $decoded;
}

function tba_cache_write(string $dir, string $file, $data): void
{
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        return;
    }
    if (!is_writable($dir)) {
        return;
    }
    $payload = json_encode(
        ['fetched_at' => time(), 'data' => $data],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE
    );
    if ($payload === false) {
        return;
    }
    $tmp = $file . '.' . bin2hex(random_bytes(4)) . '.tmp';
    if (@file_put_contents($tmp, $payload, LOCK_EX) === false) {
        @unlink($tmp);
        return;
    }
    if (!@rename($tmp, $file)) {
        @unlink($tmp);
    }
}

/** Eski önbellek dosyalarını ara sıra temizler (her isteğin yaklaşık %2'sinde). */
function tba_cache_gc(string $dir, int $maxAge): void
{
    if (random_int(1, 50) !== 1 || !is_dir($dir)) {
        return;
    }
    $limit = time() - max(3600, $maxAge);
    foreach (glob(rtrim($dir, '/\\') . '/tba_*') ?: [] as $file) {
        $mtime = @filemtime($file);
        if ($mtime !== false && $mtime < $limit) {
            @unlink($file);
        }
    }
}

// ---------------------------------------------------------------------------
// TBA HTTP istemcisi (cURL)
// ---------------------------------------------------------------------------

/**
 * @return array{status:int,data:mixed}  404 durumunda data = null döner.
 * @throws OctoHttpException
 */
function tba_fetch(string $path, array $cfg): array
{
    $apiKey = (string)($cfg['tba_api_key'] ?? '');
    if ($apiKey === '' || str_starts_with($apiKey, 'BURAYA')) {
        throw new OctoHttpException('TBA API anahtarı yapılandırılmamış (api/config.local.php).', 500);
    }
    if (!function_exists('curl_init')) {
        throw new OctoHttpException('Sunucuda cURL eklentisi yüklü değil.', 500);
    }

    $url = rtrim((string)$cfg['tba_base_url'], '/') . $path;
    $ch  = curl_init($url);
    if ($ch === false) {
        throw new OctoHttpException('cURL başlatılamadı.', 500);
    }

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_TIMEOUT        => max(3, (int)($cfg['http_timeout'] ?? 12)),
        CURLOPT_ENCODING       => '',
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_HTTPHEADER     => [
            'X-TBA-Auth-Key: ' . $apiKey,
            'Accept: application/json',
            'User-Agent: Octo-Replay/1.0 (FRC 7682 Fusion Robotics)',
        ],
    ]);

    $body   = curl_exec($ch);
    $errno  = curl_errno($ch);
    $error  = curl_error($ch);
    $status = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    unset($ch);

    if ($body === false || $errno !== 0) {
        error_log("[octo-replay] TBA cURL hatası ({$errno}): {$error} · {$path}");
        throw new OctoHttpException('TBA sunucusuna ulaşılamadı.', 502);
    }

    if ($status === 404) {
        return ['status' => 404, 'data' => null];
    }
    if ($status === 401 || $status === 403) {
        error_log("[octo-replay] TBA anahtarı reddedildi (HTTP {$status})");
        throw new OctoHttpException('TBA API anahtarı geçersiz veya yetkisiz.', 502);
    }
    if ($status === 429) {
        throw new OctoHttpException('TBA istek sınırına ulaşıldı, biraz sonra tekrar deneyin.', 503);
    }
    if ($status < 200 || $status >= 300) {
        error_log("[octo-replay] TBA beklenmeyen HTTP durumu {$status} · {$path}");
        throw new OctoHttpException("TBA beklenmeyen bir yanıt verdi (HTTP {$status}).", 502);
    }

    $data = json_decode((string)$body, true);
    if (json_last_error() !== JSON_ERROR_NONE) {
        error_log('[octo-replay] TBA JSON ayrıştırma hatası: ' . json_last_error_msg() . " · {$path}");
        throw new OctoHttpException('TBA yanıtı okunamadı.', 502);
    }
    return ['status' => $status, 'data' => $data];
}

/**
 * Önbellekten okur; yoksa/eskiyse TBA'dan çeker, dönüştürür ve yazar.
 *
 * @return array{data:mixed,cached:bool,stale:bool,fetched_at:int}
 * @throws OctoHttpException
 */
function tba_cached(array $cfg, string $cacheKey, string $path, ?callable $transform = null): array
{
    $ttl      = max(1, (int)($cfg['cache_ttl'] ?? 600));
    $staleTtl = max($ttl, (int)($cfg['stale_ttl'] ?? 86400));
    $dir      = (string)($cfg['cache_dir'] ?? (dirname(__DIR__) . '/cache'));
    $file     = tba_cache_path($dir, $cacheKey);
    $cached   = tba_cache_read($file);
    $now      = time();

    if ($cached !== null) {
        $age = $now - (int)$cached['fetched_at'];
        if ($age >= 0 && $age < $ttl) {
            return ['data' => $cached['data'], 'cached' => true, 'stale' => false, 'fetched_at' => (int)$cached['fetched_at']];
        }
    }

    try {
        $res  = tba_fetch($path, $cfg);
        $data = $res['data'];
        if ($transform !== null) {
            $data = $transform($data);
        }
        tba_cache_write($dir, $file, $data);
        tba_cache_gc($dir, $staleTtl);
        return ['data' => $data, 'cached' => false, 'stale' => false, 'fetched_at' => $now];
    } catch (OctoHttpException $e) {
        if ($cached !== null && ($now - (int)$cached['fetched_at']) < $staleTtl) {
            return ['data' => $cached['data'], 'cached' => true, 'stale' => true, 'fetched_at' => (int)$cached['fetched_at']];
        }
        throw $e;
    }
}

// ---------------------------------------------------------------------------
// Dönüştürücüler (TBA -> frontend'in ihtiyaç duyduğu sade yapı)
// ---------------------------------------------------------------------------

function tba_team_number(string $teamKey): int
{
    return (int)preg_replace('/\D+/', '', $teamKey);
}

function tba_str($value): string
{
    return is_scalar($value) ? (string)$value : '';
}

function tba_int_or_null($value): ?int
{
    return (is_int($value) || (is_string($value) && ctype_digit($value))) ? (int)$value : (is_float($value) ? (int)$value : null);
}

function tba_map_event(array $e): array
{
    return [
        'key'               => tba_str($e['key'] ?? ''),
        'name'              => tba_str($e['name'] ?? ''),
        'short_name'        => tba_str($e['short_name'] ?? ($e['name'] ?? '')),
        'city'              => tba_str($e['city'] ?? ''),
        'state_prov'        => tba_str($e['state_prov'] ?? ''),
        'country'           => tba_str($e['country'] ?? ''),
        'event_type'        => (int)($e['event_type'] ?? -1),
        'event_type_string' => tba_str($e['event_type_string'] ?? ''),
        'playoff_type'      => tba_int_or_null($e['playoff_type'] ?? null),
        'start_date'        => tba_str($e['start_date'] ?? ''),
        'end_date'          => tba_str($e['end_date'] ?? ''),
        'week'              => tba_int_or_null($e['week'] ?? null),
        'year'              => (int)($e['year'] ?? 0),
        'website'           => tba_str($e['website'] ?? ''),
        'is_offseason'      => in_array((int)($e['event_type'] ?? -1), [99, 100], true),
    ];
}

function tba_transform_events(array $allowedCountries): callable
{
    $allowed = array_map('octo_lower', $allowedCountries);
    return static function ($raw) use ($allowed): array {
        $out = [];
        if (!is_array($raw)) {
            return $out;
        }
        foreach ($raw as $event) {
            if (!is_array($event)) {
                continue;
            }
            $country = octo_lower(trim(tba_str($event['country'] ?? '')));
            if ($country === '' || !in_array($country, $allowed, true)) {
                continue;
            }
            $mapped = tba_map_event($event);
            if ($mapped['key'] !== '') {
                $out[] = $mapped;
            }
        }
        usort($out, static function (array $a, array $b): int {
            return [$a['start_date'], $a['name']] <=> [$b['start_date'], $b['name']];
        });
        return $out;
    };
}

function tba_transform_event($raw): ?array
{
    return is_array($raw) ? tba_map_event($raw) : null;
}

function tba_transform_teams($raw): array
{
    $out = [];
    if (!is_array($raw)) {
        return $out;
    }
    foreach ($raw as $team) {
        if (!is_array($team)) {
            continue;
        }
        $number = (int)($team['team_number'] ?? 0);
        if ($number <= 0) {
            continue;
        }
        $out[] = [
            'key'         => 'frc' . $number,
            'team_number' => $number,
            'nickname'    => tba_str($team['nickname'] ?? ''),
            'city'        => tba_str($team['city'] ?? ''),
            'state_prov'  => tba_str($team['state_prov'] ?? ''),
            'country'     => tba_str($team['country'] ?? ''),
        ];
    }
    usort($out, static fn(array $a, array $b): int => $a['team_number'] <=> $b['team_number']);
    return $out;
}

function tba_transform_rankings($raw): array
{
    $result = ['sort_order_info' => [], 'rankings' => []];
    if (!is_array($raw)) {
        return $result;
    }
    foreach (($raw['sort_order_info'] ?? []) as $info) {
        $result['sort_order_info'][] = is_array($info) ? tba_str($info['name'] ?? '') : '';
    }
    foreach (($raw['rankings'] ?? []) as $row) {
        if (!is_array($row)) {
            continue;
        }
        $teamKey = tba_str($row['team_key'] ?? '');
        $record  = is_array($row['record'] ?? null) ? $row['record'] : [];
        $sort    = [];
        foreach ((is_array($row['sort_orders'] ?? null) ? $row['sort_orders'] : []) as $value) {
            $sort[] = is_numeric($value) ? $value + 0 : null;
        }
        $result['rankings'][] = [
            'rank'           => (int)($row['rank'] ?? 0),
            'team_key'       => $teamKey,
            'team_number'    => tba_team_number($teamKey),
            'wins'           => (int)($record['wins'] ?? 0),
            'losses'         => (int)($record['losses'] ?? 0),
            'ties'           => (int)($record['ties'] ?? 0),
            'matches_played' => (int)($row['matches_played'] ?? 0),
            'dq'             => (int)($row['dq'] ?? 0),
            'sort_orders'    => $sort,
        ];
    }
    return $result;
}

function tba_map_alliance($raw): array
{
    $raw = is_array($raw) ? $raw : [];
    $keys = [];
    foreach ((is_array($raw['team_keys'] ?? null) ? $raw['team_keys'] : []) as $key) {
        if (is_string($key) && preg_match('/^frc\d{1,5}$/', $key) === 1) {
            $keys[] = $key;
        }
    }
    $score = $raw['score'] ?? null;
    return [
        'team_keys'           => $keys,
        'score'               => is_numeric($score) ? (int)$score : null,
        'surrogate_team_keys' => array_values(array_filter((array)($raw['surrogate_team_keys'] ?? []), 'is_string')),
        'dq_team_keys'        => array_values(array_filter((array)($raw['dq_team_keys'] ?? []), 'is_string')),
    ];
}

function tba_map_match(array $m, bool $withBreakdown = false): array
{
    $videos = [];
    foreach ((is_array($m['videos'] ?? null) ? $m['videos'] : []) as $video) {
        if (is_array($video) && isset($video['type'], $video['key']) && is_string($video['key'])) {
            $videos[] = ['type' => tba_str($video['type']), 'key' => $video['key']];
        }
    }
    $alliances = is_array($m['alliances'] ?? null) ? $m['alliances'] : [];
    $out = [
        'key'              => tba_str($m['key'] ?? ''),
        'event_key'        => tba_str($m['event_key'] ?? ''),
        'comp_level'       => tba_str($m['comp_level'] ?? ''),
        'set_number'       => (int)($m['set_number'] ?? 0),
        'match_number'     => (int)($m['match_number'] ?? 0),
        'alliances'        => [
            'red'  => tba_map_alliance($alliances['red'] ?? null),
            'blue' => tba_map_alliance($alliances['blue'] ?? null),
        ],
        'winning_alliance' => tba_str($m['winning_alliance'] ?? ''),
        'time'             => tba_int_or_null($m['time'] ?? null),
        'predicted_time'   => tba_int_or_null($m['predicted_time'] ?? null),
        'actual_time'      => tba_int_or_null($m['actual_time'] ?? null),
        'post_result_time' => tba_int_or_null($m['post_result_time'] ?? null),
        'videos'           => $videos,
    ];
    if ($withBreakdown) {
        $out['score_breakdown'] = is_array($m['score_breakdown'] ?? null) ? $m['score_breakdown'] : null;
    }
    return $out;
}

function tba_transform_matches($raw): array
{
    $out = [];
    if (!is_array($raw)) {
        return $out;
    }
    foreach ($raw as $match) {
        if (!is_array($match)) {
            continue;
        }
        $mapped = tba_map_match($match);
        if ($mapped['key'] !== '') {
            $out[] = $mapped;
        }
    }
    return $out;
}

function tba_transform_match($raw): ?array
{
    return is_array($raw) ? tba_map_match($raw, true) : null;
}

// ---------------------------------------------------------------------------
// Teşhis: ?action=diag  (gizli bilgi döndürmez; 10 sn'de en fazla 1 kez çalışır)
// ---------------------------------------------------------------------------

function tba_diagnose(array $cfg): void
{
    $dir = (string)($cfg['cache_dir'] ?? (dirname(__DIR__) . '/cache'));
    $throttle = rtrim($dir, '/\\') . '/diag.stamp';
    $last = is_file($throttle) ? (int)@filemtime($throttle) : 0;
    if (time() - $last < 10) {
        octo_fail('Teşhis çok sık çalıştırıldı, 10 saniye bekleyin.', 429, [], ['Retry-After' => '10']);
    }
    @touch($throttle);

    $key    = (string)($cfg['tba_api_key'] ?? '');
    $report = [
        'php'                => PHP_VERSION,
        'curl_extension'     => function_exists('curl_init'),
        'config_local_found' => is_file(__DIR__ . '/config.local.php'),
        'key_configured'     => $key !== '' && !str_starts_with($key, 'BURAYA'),
        'key_length'         => strlen($key),
        'key_length_expected' => 64,
        'cache_dir_exists'   => is_dir($dir),
        'cache_dir_writable' => is_dir($dir) && is_writable($dir),
    ];

    try {
        $status = tba_fetch('/status', $cfg);
        $report['tba_status'] = ['ok' => true, 'http' => $status['status']];
    } catch (OctoHttpException $e) {
        $report['tba_status'] = ['ok' => false, 'error' => $e->getMessage()];
    }

    if (($report['tba_status']['ok'] ?? false) === true) {
        $year = (int)($cfg['default_year'] ?? 2026);
        try {
            $res   = tba_fetch("/events/{$year}", $cfg);
            $all   = is_array($res['data']) ? $res['data'] : [];
            $turkish = [];
            foreach ($all as $event) {
                $country = is_array($event) ? tba_str($event['country'] ?? '') : '';
                if (stripos($country, 'tur') !== false || stripos($country, 'türk') !== false) {
                    $turkish[$country] = ($turkish[$country] ?? 0) + 1;
                }
            }
            $kept = array_filter($all, static function ($e) use ($cfg): bool {
                $allowed = array_map('octo_lower', (array)($cfg['allowed_countries'] ?? []));
                return is_array($e) && in_array(octo_lower(trim(tba_str($e['country'] ?? ''))), $allowed, true);
            });
            $report['events'] = [
                'year'                   => $year,
                'total_from_tba'         => count($all),
                'turkey_events_matched'  => count($kept),
                'country_values_like_turkey' => $turkish,
            ];
        } catch (OctoHttpException $e) {
            $report['events'] = ['ok' => false, 'error' => $e->getMessage()];
        }
    }

    octo_respond(['ok' => true, 'diag' => $report]);
}

// ---------------------------------------------------------------------------
// İstek yönlendirme
// ---------------------------------------------------------------------------

octo_send_headers('private, max-age=60');

try {
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'GET') {
        octo_fail('Yalnızca GET desteklenir.', 405, [], ['Allow' => 'GET']);
    }

    $cfg    = octo_config();
    $action = octo_param('action', '/^[a-z_]{3,20}$/');
    $result = null;

    switch ($action) {
        case 'events':
            $year   = (int)octo_param('year', '/^20\d{2}$/', false, (string)($cfg['default_year'] ?? 2026));
            $result = tba_cached(
                $cfg,
                "events:{$year}:tr",
                "/events/{$year}",
                tba_transform_events((array)($cfg['allowed_countries'] ?? ['Turkey']))
            );
            break;

        case 'event':
            $event  = (string)octo_param('event', TBA_EVENT_KEY_RE);
            $result = tba_cached($cfg, "event:{$event}", "/event/{$event}", 'tba_transform_event');
            if ($result['data'] === null) {
                octo_fail('Etkinlik bulunamadı.', 404);
            }
            break;

        case 'teams':
            $event  = (string)octo_param('event', TBA_EVENT_KEY_RE);
            $result = tba_cached($cfg, "teams:{$event}", "/event/{$event}/teams/simple", 'tba_transform_teams');
            break;

        case 'rankings':
            $event  = (string)octo_param('event', TBA_EVENT_KEY_RE);
            $result = tba_cached($cfg, "rankings:{$event}", "/event/{$event}/rankings", 'tba_transform_rankings');
            break;

        case 'matches':
            $event  = (string)octo_param('event', TBA_EVENT_KEY_RE);
            $result = tba_cached($cfg, "matches:{$event}", "/event/{$event}/matches", 'tba_transform_matches');
            break;

        case 'match':
            $match  = (string)octo_param('match', TBA_MATCH_KEY_RE);
            $result = tba_cached($cfg, "match:{$match}", "/match/{$match}", 'tba_transform_match');
            if ($result['data'] === null) {
                octo_fail('Maç bulunamadı.', 404);
            }
            break;

        case 'team_matches':
            $team   = (int)octo_param('team', '/^\d{1,5}$/');
            $year   = (int)octo_param('year', '/^20\d{2}$/', false, (string)($cfg['default_year'] ?? 2026));
            if ($team < 1) {
                octo_fail("'team' parametresi geçersiz.", 400);
            }
            $result = tba_cached($cfg, "team_matches:{$team}:{$year}", "/team/frc{$team}/matches/{$year}", 'tba_transform_matches');
            break;

        case 'diag':
            tba_diagnose($cfg);
            break;

        default:
            octo_fail('Bilinmeyen action.', 400, ['actions' => ['events', 'event', 'teams', 'rankings', 'matches', 'match', 'team_matches', 'diag']]);
    }

    octo_respond([
        'ok'   => true,
        'data' => $result['data'],
        'meta' => [
            'cached'     => $result['cached'],
            'stale'      => $result['stale'],
            'fetched_at' => $result['fetched_at'],
            'ttl'        => (int)($cfg['cache_ttl'] ?? 600),
        ],
    ]);
} catch (OctoHttpException $e) {
    $headers = $e->httpStatus() === 503 ? ['Retry-After' => '30'] : [];
    octo_fail($e->getMessage(), $e->httpStatus(), [], $headers);
} catch (Throwable $e) {
    error_log('[octo-replay] tba-proxy beklenmeyen hata: ' . $e->getMessage() . ' @ ' . $e->getFile() . ':' . $e->getLine());
    octo_fail('Sunucu hatası.', 500);
}
