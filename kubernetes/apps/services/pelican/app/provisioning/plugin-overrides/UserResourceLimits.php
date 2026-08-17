<?php

namespace Boy132\UserCreatableServers\Models;

use App\Exceptions\Service\Deployment\NoViableAllocationException;
use App\Exceptions\Service\Deployment\NoViableNodeException;
use App\Models\Allocation;
use App\Models\Egg;
use App\Models\Server;
use App\Models\User;
use App\Services\Deployment\FindViableNodesService;
use App\Services\Servers\ServerCreationService;
use Carbon\Carbon;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Support\Facades\Cache;
use RuntimeException;

/**
 * @property int $id
 * @property User $user
 * @property int $user_id
 * @property int $cpu
 * @property int $memory
 * @property int $disk
 * @property ?int $server_limit
 * @property Carbon $created_at
 * @property Carbon $updated_at
 */
class UserResourceLimits extends Model
{
    protected $fillable = [
        'user_id',
        'cpu',
        'memory',
        'disk',
        'server_limit',
    ];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }

    public function getCpuLeft(): ?int
    {
        if ($this->cpu > 0) {
            $sum_cpu = $this->user->servers->sum('cpu');

            return (int) max(0, $this->cpu - $sum_cpu);
        }

        return null;
    }

    public function getMemoryLeft(): ?int
    {
        if ($this->memory > 0) {
            $sum_memory = $this->user->servers->sum('memory');

            return (int) max(0, $this->memory - $sum_memory);
        }

        return null;
    }

    public function getDiskLeft(): ?int
    {
        if ($this->disk > 0) {
            $sum_disk = $this->user->servers->sum('disk');

            return (int) max(0, $this->disk - $sum_disk);
        }

        return null;
    }

    public function canCreateServer(int $cpu, int $memory, int $disk): bool
    {
        if ($this->server_limit && $this->user->servers->count() >= $this->server_limit) {
            return false;
        }

        if ($this->cpu > 0) {
            if ($cpu <= 0) {
                return false;
            }

            $sum_cpu = $this->user->servers->sum('cpu');
            if ($sum_cpu + $cpu > $this->cpu) {
                return false;
            }
        }

        if ($this->memory > 0) {
            if ($memory <= 0) {
                return false;
            }

            $sum_memory = $this->user->servers->sum('memory');
            if ($sum_memory + $memory > $this->memory) {
                return false;
            }
        }

        if ($this->disk > 0) {
            if ($disk <= 0) {
                return false;
            }

            $sum_disk = $this->user->servers->sum('disk');
            if ($sum_disk + $disk > $this->disk) {
                return false;
            }
        }

        return true;
    }

    /** @param array<string, mixed> $variables */
    public function createServer(string $name, int|Egg $egg, int $cpu, int $memory, int $disk, array $variables = []): Server|false
    {
        if (!$this->canCreateServer($cpu, $memory, $disk)) {
            return false;
        }

        if (!$egg instanceof Egg) {
            $egg = Egg::findOrFail($egg);
        }

        $allowedEggs = array_map('strval', array_filter(explode(',', (string) config('user-creatable-servers.allowed_eggs'))));
        if (!in_array((string) $egg->id, $allowedEggs, true)) {
            throw new RuntimeException('This egg is not approved for self-service deployment.');
        }

        $profiles = config('user-creatable-servers.network_profiles', []);
        $profile = $profiles[$egg->uuid] ?? null;
        if (!is_array($profile)) {
            throw new RuntimeException("No self-service network profile exists for egg {$egg->uuid}.");
        }

        $requiredPorts = max(1, (int) ($profile['ports'] ?? 1));
        $portVariables = is_array($profile['variables'] ?? null) ? $profile['variables'] : [];

        return Cache::lock('user-creatable-servers:allocation-block', 120)->block(15, function () use (
            $name,
            $egg,
            $cpu,
            $memory,
            $disk,
            $variables,
            $requiredPorts,
            $portVariables,
        ): Server {
            $block = $this->selectAllocationBlock($cpu, $memory, $disk, $requiredPorts);

            $environment = [];
            foreach ($egg->variables as $variable) {
                $environment[$variable->env_variable] = $variables[$variable->env_variable] ?? $variable->default_value;
            }

            foreach ($portVariables as $environmentVariable => $offset) {
                $offset = (int) $offset;
                if ($offset < 0 || $offset >= count($block)) {
                    throw new RuntimeException("Invalid allocation offset for {$environmentVariable}.");
                }
                if (!array_key_exists($environmentVariable, $environment)) {
                    throw new RuntimeException("Network profile references missing variable {$environmentVariable}.");
                }

                $environment[$environmentVariable] = (string) $block[$offset]->port;
            }

            $data = [
                'name' => $name,
                'owner_id' => $this->user_id,
                'egg_id' => $egg->id,
                'node_id' => $block[0]->node_id,
                'allocation_id' => $block[0]->id,
                'allocation_additional' => collect(array_slice($block, 1))->pluck('id')->all(),
                'cpu' => $cpu,
                'memory' => $memory,
                'disk' => $disk,
                'swap' => 0,
                'io' => 500,
                'environment' => $environment,
                'skip_scripts' => false,
                'start_on_completion' => true,
                'oom_killer' => false,
                'database_limit' => config('user-creatable-servers.database_limit'),
                'allocation_limit' => max((int) config('user-creatable-servers.allocation_limit'), $requiredPorts - 1),
                'backup_limit' => config('user-creatable-servers.backup_limit'),
            ];

            /** @var ServerCreationService $service */
            $service = app(ServerCreationService::class);

            return $service->handle($data);
        });
    }

    /** @return array<int, Allocation> */
    private function selectAllocationBlock(int $cpu, int $memory, int $disk, int $requiredPorts): array
    {
        $tags = array_filter(explode(',', (string) config('user-creatable-servers.deployment_tags')));
        $nodeIds = app(FindViableNodesService::class)
            ->handle($memory, $disk, $cpu, $tags)
            ->pluck('id');

        if ($nodeIds->isEmpty()) {
            throw new NoViableNodeException(trans('exceptions.deployment.no_viable_nodes'));
        }

        $ranges = $this->deploymentPortRanges();
        if ($ranges === []) {
            throw new RuntimeException('Self-service deployment ports are not configured.');
        }

        foreach ($nodeIds as $nodeId) {
            $query = Allocation::query()
                ->where('node_id', $nodeId)
                ->whereNull('server_id')
                ->where('is_locked', false)
                ->where(function ($inner) use ($ranges) {
                    foreach ($ranges as [$first, $last]) {
                        $inner->orWhereBetween('port', [$first, $last]);
                    }
                })
                ->orderBy('ip')
                ->orderBy('port');

            foreach ($query->get()->groupBy('ip') as $allocations) {
                $block = [];
                $expectedPort = null;

                foreach ($allocations as $allocation) {
                    if ($expectedPort !== null && $allocation->port !== $expectedPort) {
                        $block = [];
                    }

                    $block[] = $allocation;
                    $expectedPort = $allocation->port + 1;

                    if (count($block) === $requiredPorts) {
                        return array_values($block);
                    }
                }
            }
        }

        throw new NoViableAllocationException(trans('exceptions.deployment.no_viable_allocations'));
    }

    /** @return array<int, array{0: int, 1: int}> */
    private function deploymentPortRanges(): array
    {
        $ranges = [];
        foreach (array_filter(explode(',', (string) config('user-creatable-servers.deployment_ports'))) as $value) {
            $value = trim($value);
            if (preg_match('/^(\d+)-(\d+)$/', $value, $matches)) {
                $first = (int) $matches[1];
                $last = (int) $matches[2];
                if ($first <= $last) {
                    $ranges[] = [$first, $last];
                }
            } elseif (ctype_digit($value)) {
                $port = (int) $value;
                $ranges[] = [$port, $port];
            }
        }

        return $ranges;
    }
}
