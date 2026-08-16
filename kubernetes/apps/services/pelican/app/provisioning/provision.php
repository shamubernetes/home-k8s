<?php

declare(strict_types=1);

use App\Enums\EggFormat;
use App\Models\Egg;
use App\Models\Node;
use App\Services\Eggs\Sharing\EggImporterService;
use App\Traits\EnvironmentWriterTrait;
use Illuminate\Contracts\Console\Kernel;

require '/var/www/html/vendor/autoload.php';

$app = require '/var/www/html/bootstrap/app.php';
$app->make(Kernel::class)->bootstrap();

$catalog = [
    'b2c6d19f-3d24-41e3-802d-c2bfbe63b519' => '/provisioning/project-zomboid.json',
    'b5ec0ac5-40f1-4739-9e68-91d71c7fd074' => '/provisioning/windrose.json',
];

/** @var EggImporterService $importer */
$importer = $app->make(EggImporterService::class);
$allowedEggIds = [];
$importedEggs = [];

foreach ($catalog as $uuid => $path) {
    $content = file_get_contents($path);
    if ($content === false) {
        throw new RuntimeException("Unable to read egg definition: {$path}");
    }

    $parsed = json_decode($content, true, 512, JSON_THROW_ON_ERROR);
    if (($parsed['uuid'] ?? null) !== $uuid) {
        throw new RuntimeException("Egg UUID does not match the catalog entry: {$path}");
    }

    $existing = Egg::query()->where('uuid', $uuid)->first();
    $egg = $importer->fromContent($content, EggFormat::JSON, $existing);

    $allowedEggIds[] = $egg->id;
    $importedEggs[] = ['id' => $egg->id, 'name' => $egg->name, 'uuid' => $egg->uuid];
}

$node = Node::query()->where('fqdn', 'mnemosyne.thezoo.house')->sole();
$nodeTags = collect($node->tags ?? [])
    ->push('user_creatable_servers')
    ->filter()
    ->unique()
    ->values()
    ->all();
$node->forceFill(['tags' => $nodeTags])->save();

$environmentWriter = new class
{
    use EnvironmentWriterTrait;
};

$environmentWriter->writeToEnvironment([
    'UCS_DEFAULT_DATABASE_LIMIT' => '0',
    'UCS_DEFAULT_ALLOCATION_LIMIT' => '2',
    'UCS_DEFAULT_BACKUP_LIMIT' => '3',
    'UCS_CAN_USERS_UPDATE_SERVERS' => 'true',
    'UCS_CAN_USERS_DELETE_SERVERS' => 'true',
    'UCS_DEPLOYMENT_TAGS' => 'user_creatable_servers',
    'UCS_DEPLOYMENT_PORTS' => '',
    'UCS_ALLOWED_EGGS' => implode(',', $allowedEggIds),
]);

fwrite(STDOUT, json_encode([
    'eggs' => $importedEggs,
    'node' => ['id' => $node->id, 'name' => $node->name, 'tags' => $nodeTags],
], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . PHP_EOL);
