#!/usr/bin/env php
<?php

declare(strict_types=1);

use PPFlight\InvoiceRenderer\InvoiceRenderer;
use PPFlight\InvoiceRenderer\RenderException;
use PPFlight\InvoiceRenderer\SnapshotValidator;

const RENDERER_ROOT = __DIR__ . '/..';

function fail(string $code): never
{
    fwrite(STDERR, "PPFLIGHT_RENDERER_ERROR=" . $code . PHP_EOL);
    exit(1);
}

function absoluteDirectory(string $path): string
{
    if ($path === '' || $path[0] !== '/' || str_contains($path, "\0") || !is_dir($path) || is_link($path) || !is_writable($path)) {
        fail('cache');
    }
    $real = realpath($path);
    if ($real === false) {
        fail('cache');
    }
    return $real;
}

$options = getopt('', ['input:', 'output:', 'cache-dir:']);
if (!isset($options['output'], $options['cache-dir']) || !is_string($options['output']) || !is_string($options['cache-dir'])) {
    fail('input');
}
if (isset($options['input']) && (!is_string($options['input']) || $options['input'] === '' || $options['input'][0] !== '/')) {
    fail('input');
}

$autoload = RENDERER_ROOT . '/vendor/autoload.php';
if (!is_file($autoload)) {
    $testAutoload = getenv('PPFLIGHT_DOMPDF_TEST_AUTOLOAD');
    if (is_string($testAutoload) && str_starts_with($testAutoload, '/') && is_file($testAutoload)) {
        $autoload = $testAutoload;
    } else {
        fail('dependencies');
    }
}
require $autoload;
if (!class_exists(SnapshotValidator::class)) {
    spl_autoload_register(static function (string $class): void {
        $prefix = 'PPFlight\\InvoiceRenderer\\';
        if (!str_starts_with($class, $prefix)) {
            return;
        }
        $name = substr($class, strlen($prefix));
        if (preg_match('/^[A-Za-z][A-Za-z0-9_]*$/', $name) !== 1) {
            return;
        }
        $file = RENDERER_ROOT . '/src/' . $name . '.php';
        if (is_file($file)) {
            require $file;
        }
    });
}

try {
    if (isset($options['input'])) {
        if (!is_file($options['input']) || is_link($options['input'])) {
            throw new RenderException('--input must be a regular, non-symlink file.');
        }
        $input = file_get_contents($options['input'], false, null, 0, SnapshotValidator::MAX_INPUT_BYTES + 1);
    } else {
        $input = stream_get_contents(STDIN, SnapshotValidator::MAX_INPUT_BYTES + 1);
    }
    if ($input === false) {
        throw new RenderException('Could not read input.');
    }
    $snapshot = (new SnapshotValidator())->decodeAndValidate($input);
    $assets = realpath(RENDERER_ROOT . '/assets');
    if ($assets === false || is_link($assets)) {
        throw new RenderException('renderer assets directory is unavailable.', 'dependencies');
    }
    $cache = absoluteDirectory($options['cache-dir']);
    $result = (new InvoiceRenderer($assets, $cache))->render($snapshot, $options['output']);
    fwrite(STDOUT, json_encode($result, JSON_THROW_ON_ERROR) . PHP_EOL);
} catch (RenderException $exception) {
    fail($exception->diagnosticCode());
} catch (JsonException $exception) {
    fail('internal');
} catch (Throwable $exception) {
    fail('internal');
}
