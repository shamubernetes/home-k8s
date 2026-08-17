<?php

declare(strict_types=1);

use App\Enums\EggFormat;
use App\Models\Allocation;
use App\Models\Egg;
use App\Models\Node;
use App\Models\Role;
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

$poolIp = '10.100.47.100';
$poolFirstPort = 28000;
$poolLastPort = 28999;
$poolPorts = range($poolFirstPort, $poolLastPort);

$conflictingPoolAllocation = Allocation::query()
    ->where('node_id', $node->id)
    ->whereBetween('port', [$poolFirstPort, $poolLastPort])
    ->where('ip', '!=', $poolIp)
    ->first();
if ($conflictingPoolAllocation !== null) {
    throw new RuntimeException("Port {$conflictingPoolAllocation->port} in the self-service pool already belongs to another IP.");
}

$existingPoolPorts = Allocation::query()
    ->where('node_id', $node->id)
    ->where('ip', $poolIp)
    ->whereBetween('port', [$poolFirstPort, $poolLastPort])
    ->pluck('port')
    ->map(static fn ($port) => (int) $port)
    ->all();
$missingPoolPorts = array_values(array_diff($poolPorts, $existingPoolPorts));
$now = now();

foreach (array_chunk($missingPoolPorts, 250) as $ports) {
    Allocation::query()->insert(array_map(static fn (int $port): array => [
        'node_id' => $node->id,
        'ip' => $poolIp,
        'port' => $port,
        'server_id' => null,
        'ip_alias' => $node->fqdn,
        'notes' => 'Friends self-service public game pool',
        'is_locked' => false,
        'created_at' => $now,
        'updated_at' => $now,
    ], $ports));
}

$poolAllocationCount = Allocation::query()
    ->where('node_id', $node->id)
    ->where('ip', $poolIp)
    ->whereBetween('port', [$poolFirstPort, $poolLastPort])
    ->count();
if ($poolAllocationCount !== count($poolPorts)) {
    throw new RuntimeException("Expected 1000 self-service allocations, found {$poolAllocationCount}.");
}

// Every imported game egg is eligible for self-service. Database images are
// intentionally excluded because friends receive game-server ownership, not
// infrastructure administration. New game eggs automatically receive a
// single-port profile, plus one allocation for each explicit *_PORT variable.
$databaseEggPattern = '/(?:^|[^a-z])(redis|postgres(?:ql)?|mongo(?:db)?|maria(?:db)?|mysql|database)(?:[^a-z]|$)/i';
$profileOverrides = [
    'Counter-Strike 2' => ['ports' => 2, 'variables' => ['TV_PORT' => 1]],
    'Palworld' => ['ports' => 2, 'variables' => ['RCON_PORT' => 1]],
    'Project Zomboid' => ['ports' => 12, 'variables' => ['STEAM_PORT' => 1]],
    'American Truck Simulator Dedicated Server' => ['ports' => 2, 'variables' => ['QUERY_PORT' => 1]],
    'Euro Truck Simulator 2 Dedicated server' => ['ports' => 2, 'variables' => ['QUERY_PORT' => 1]],
    'Ark: Survival Evolved' => ['ports' => 4, 'variables' => ['QUERY_PORT' => 1, 'RCON_PORT' => 2]],
    'Rust' => ['ports' => 4, 'variables' => ['QUERY_PORT' => 1, 'RCON_PORT' => 2, 'APP_PORT' => 3]],
    'Satisfactory' => ['ports' => 2, 'variables' => ['RELIABLE_PORT' => 1]],
    'Space Engineers' => ['ports' => 3, 'variables' => ['STEAM_PORT' => 1, 'REMOTEAPI_PORT' => 2]],
    'Valheim' => ['ports' => 3, 'variables' => []],
];

$allowedEggIds = [];
$allowedEggs = [];
$networkProfiles = [];

foreach (Egg::query()->with('variables')->orderBy('id')->get() as $egg) {
    if (preg_match($databaseEggPattern, $egg->name) === 1) {
        continue;
    }

    $portVariables = $egg->variables
        ->filter(static fn ($variable): bool => preg_match('/(?:^|_)PORT$/i', $variable->env_variable) === 1)
        ->values();
    $variableOffsets = [];
    foreach ($portVariables as $offset => $variable) {
        $variableOffsets[$variable->env_variable] = $offset + 1;
    }

    $profile = $profileOverrides[$egg->name] ?? [
        'ports' => 1 + count($variableOffsets),
        'variables' => $variableOffsets,
    ];
    if ($profile['ports'] < (1 + count($profile['variables']))) {
        throw new RuntimeException("Network profile for {$egg->name} does not provide enough allocations.");
    }

    $allowedEggIds[] = $egg->id;
    $allowedEggs[] = ['id' => $egg->id, 'name' => $egg->name, 'uuid' => $egg->uuid];
    $networkProfiles[$egg->uuid] = [
        'name' => $egg->name,
        'ports' => $profile['ports'],
        'variables' => $profile['variables'],
    ];
}

// Pelican roles grant administrator capabilities. Friends get no admin
// permissions here because owners already receive every permission on their
// own servers. Keeping this role empty preserves full self-service access
// without exposing the panel, other servers, nodes, users, roles, or plugins.
$friendsRole = Role::findOrCreate('Friends', Role::DEFAULT_GUARD_NAME);
$friendsRole->syncPermissions([]);
$friendsRole->nodes()->sync([$node->id]);

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
    'UCS_DEPLOYMENT_PORTS' => "{$poolFirstPort}-{$poolLastPort}",
    'UCS_ALLOWED_EGGS' => implode(',', $allowedEggIds),
    'UCS_NETWORK_PROFILES' => json_encode($networkProfiles, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
]);

fwrite(STDOUT, json_encode([
    'eggs' => $importedEggs,
    'allowed_eggs' => $allowedEggs,
    'network_profiles' => $networkProfiles,
    'allocation_pool' => [
        'node_id' => $node->id,
        'ip' => $poolIp,
        'first_port' => $poolFirstPort,
        'last_port' => $poolLastPort,
        'allocations' => $poolAllocationCount,
        'created' => count($missingPoolPorts),
    ],
    'node' => ['id' => $node->id, 'name' => $node->name, 'tags' => $nodeTags],
    'role' => [
        'id' => $friendsRole->id,
        'name' => $friendsRole->name,
        'permissions' => $friendsRole->permissions()->pluck('name')->all(),
        'nodes' => $friendsRole->nodes()->pluck('nodes.id')->all(),
    ],
], JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES) . PHP_EOL);
