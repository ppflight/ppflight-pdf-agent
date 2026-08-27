#!/usr/bin/env php
<?php

declare(strict_types=1);

use PPFlight\InvoiceRenderer\InvoiceRenderer;
use PPFlight\InvoiceRenderer\RenderException;
use PPFlight\InvoiceRenderer\SnapshotValidator;

const RENDERER_ROOT = __DIR__ . '/..';

function fail(string $message): never
{
    fwrite(STDERR, "renderer: " . $message . PHP_EOL);
    exit(1);
}

function absoluteDirectory(string $path, string $label): string
{
    if ($path === '' || $path[0] !== '/' || str_contains($path, "\0") || !is_dir($path) || is_link($path) || !is_writable($path)) {
        fail($label . ' must be an existing writable, non-symlink absolute directory.');
    }
    $real = realpath($path);
    if ($real === false) {
        fail($label . ' cannot be resolved.');
    }
    return $real;
}

$options = getopt('', ['input:', 'output:', 'cache-dir:']);
if (!isset($options['output'], $options['cache-dir']) || !is_string($options['output']) || !is_string($options['cache-dir'])) {
    fail('usage: render.php --output ABSOLUTE_PATH --cache-dir ABSOLUTE_DIRECTORY [--input ABSOLUTE_PATH]');
}
if (isset($options['input']) && (!is_string($options['input']) || $options['input'] === '' || $options['input'][0] !== '/')) {
    fail('--input must be an absolute path.');
}

$autoload = RENDERER_ROOT . '/vendor/autoload.php';
if (!is_file($autoload)) {
    $testAutoload = getenv('PPFLIGHT_DOMPDF_TEST_AUTOLOAD');
    if (is_string($testAutoload) && str_starts_with($testAutoload, '/') && is_file($testAutoload)) {
        $autoload = $testAutoload;
    } else {
        fail('renderer dependencies are not installed (expected renderer/vendor/autoload.php).');
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
        throw new RenderException('renderer assets directory is unavailable.');
    }
    $cache = absoluteDirectory($options['cache-dir'], '--cache-dir');
    $result = (new InvoiceRenderer($assets, $cache))->render($snapshot, $options['output']);
    fwrite(STDOUT, json_encode($result, JSON_THROW_ON_ERROR) . PHP_EOL);
} catch (RenderException|JsonException $exception) {
    fail($exception->getMessage());
} catch (Throwable $exception) {
    fail('render failed: ' . $exception->getMessage());
}
