import { kea } from 'kea'
import { pluginsLogicType } from 'types/scenes/plugins/pluginsLogicType'
import api from 'lib/api'
import { PluginConfigType, PluginType } from '~/types'
import { PluginRepositoryEntry, PluginTypeWithConfig } from './types'
import { userLogic } from 'scenes/userLogic'
import { getConfigSchemaObject, getPluginConfigFormData } from 'scenes/plugins/utils'

export const pluginsLogic = kea<
    pluginsLogicType<PluginType, PluginConfigType, PluginRepositoryEntry, PluginTypeWithConfig>
>({
    actions: {
        editPlugin: (id: number | null) => ({ id }),
        savePluginConfig: (pluginConfigChanges: Record<string, any>) => ({ pluginConfigChanges }),
        installPlugin: (pluginUrl: string, type: 'local' | 'custom' | 'repository') => ({ pluginUrl, type }),
        uninstallPlugin: (name: string) => ({ name }),
        setCustomPluginUrl: (customPluginUrl: string) => ({ customPluginUrl }),
        setLocalPluginUrl: (localPluginUrl: string) => ({ localPluginUrl }),
        setPluginTab: (tab: string) => ({ tab }),
        resetPluginConfigError: (id: number) => ({ id }),
    },

    loaders: ({ values }) => ({
        plugins: [
            {} as Record<number, PluginType>,
            {
                loadPlugins: async () => {
                    const { results } = await api.get('api/plugin')
                    const plugins: Record<string, PluginType> = {}
                    for (const plugin of results as PluginType[]) {
                        plugins[plugin.id] = plugin
                    }
                    return plugins
                },
                installPlugin: async ({ pluginUrl, type }) => {
                    const url = type === 'local' ? `file:${pluginUrl}` : pluginUrl
                    const response = await api.create('api/plugin', { url })
                    return { ...values.plugins, [response.id]: response }
                },
                uninstallPlugin: async () => {
                    const { plugins, editingPlugin } = values
                    if (!editingPlugin) {
                        return plugins
                    }
                    await api.delete(`api/plugin/${editingPlugin.id}`)
                    const { [editingPlugin.id]: _discard, ...rest } = plugins // eslint-disable-line
                    return rest
                },
            },
        ],
        pluginConfigs: [
            {} as Record<string, PluginConfigType>,
            {
                loadPluginConfigs: async () => {
                    const pluginConfigs: Record<string, PluginConfigType> = {}

                    const [{ results }, globalResults] = await Promise.all([
                        api.get('api/plugin_config'),
                        api.get('api/plugin_config/global_plugins/'),
                    ])

                    for (const pluginConfig of results as PluginConfigType[]) {
                        pluginConfigs[pluginConfig.plugin] = { ...pluginConfig, global: false }
                    }
                    for (const pluginConfig of globalResults as PluginConfigType[]) {
                        pluginConfigs[pluginConfig.plugin] = { ...pluginConfig, global: true }
                    }

                    return pluginConfigs
                },
                savePluginConfig: async ({ pluginConfigChanges }) => {
                    const { pluginConfigs, editingPlugin } = values

                    if (!editingPlugin) {
                        return pluginConfigs
                    }

                    const formData = getPluginConfigFormData(editingPlugin, pluginConfigChanges)

                    let response
                    if (editingPlugin.pluginConfig.id) {
                        response = await api.update(`api/plugin_config/${editingPlugin.pluginConfig.id}`, formData)
                    } else {
                        formData.append('plugin', editingPlugin.id.toString())
                        formData.append('order', '0')
                        response = await api.create(`api/plugin_config/`, formData)
                    }

                    return { ...pluginConfigs, [response.plugin]: response }
                },
                toggleEnabled: async ({ id, enabled }) => {
                    const { pluginConfigs } = values
                    const response = await api.update(`api/plugin_config/${id}`, {
                        enabled,
                    })
                    return { ...pluginConfigs, [response.plugin]: response }
                },
                resetPluginConfigError: async ({ id }) => {
                    const { pluginConfigs } = values
                    const response = await api.update(`api/plugin_config/${id}`, {
                        error: null,
                    })
                    return { ...pluginConfigs, [response.plugin]: response }
                },
            },
        ],
        repository: [
            {} as Record<string, PluginRepositoryEntry>,
            {
                loadRepository: async () => {
                    const results = await api.get('api/plugin/repository')
                    const repository: Record<string, PluginRepositoryEntry> = {}
                    for (const plugin of results as PluginRepositoryEntry[]) {
                        repository[plugin.name] = plugin
                    }
                    return repository
                },
            },
        ],
    }),

    reducers: {
        editingPluginId: [
            null as number | null,
            {
                editPlugin: (_, { id }) => id,
                savePluginConfigSuccess: () => null,
                uninstallPluginSuccess: () => null,
                installPluginSuccess: (_, { plugins }) => Object.values(plugins).pop()?.id || null,
            },
        ],
        customPluginUrl: [
            '',
            {
                setCustomPluginUrl: (_, { customPluginUrl }) => customPluginUrl,
                installPluginSuccess: () => '',
            },
        ],
        localPluginUrl: [
            '',
            {
                setLocalPluginUrl: (_, { localPluginUrl }) => localPluginUrl,
                installPluginSuccess: () => '',
            },
        ],
        pluginError: [
            null as null | string,
            {
                setCustomPluginUrl: () => null,
                installPlugin: () => null,
                installPluginFailure: (_, { error }) => error || '',
            },
        ],
        pluginConfigs: {
            uninstallPluginSuccess: (pluginConfigs, { plugins }) => {
                const newPluginConfigs: Record<number, PluginConfigType> = {}
                Object.values(pluginConfigs).forEach((pluginConfig) => {
                    if (plugins[pluginConfig.plugin]) {
                        newPluginConfigs[pluginConfig.plugin] = pluginConfig
                    }
                })
                return newPluginConfigs
            },
        },
        pluginTab: [
            'installed',
            {
                setPluginTab: (_, { tab }) => tab,
            },
        ],
    },

    selectors: {
        installedPlugins: [
            (s) => [s.plugins, s.pluginConfigs],
            (plugins, pluginConfigs): PluginTypeWithConfig[] => {
                const pluginValues = Object.values(plugins)
                return pluginValues
                    .map((plugin, index) => {
                        let pluginConfig = pluginConfigs[plugin.id]
                        if (!pluginConfig) {
                            const config: Record<string, any> = {}
                            Object.entries(getConfigSchemaObject(plugin.config_schema)).forEach(
                                ([key, { default: def }]) => {
                                    config[key] = def
                                }
                            )

                            pluginConfig = {
                                id: undefined,
                                plugin: plugin.id,
                                enabled: false,
                                config: config,
                                order: pluginValues.length + index,
                            }
                        }
                        return { ...plugin, pluginConfig }
                    })
                    .sort((a, b) => a.pluginConfig.order - b.pluginConfig.order)
                    .map((plugin, index) => ({ ...plugin, order: index + 1 }))
            },
        ],
        installedPluginNames: [
            (s) => [s.installedPlugins],
            (installedPlugins) => {
                const names: Record<string, boolean> = {}
                installedPlugins.forEach((plugin) => {
                    names[plugin.name] = true
                })
                return names
            },
        ],
        uninstalledPlugins: [
            (s) => [s.installedPluginNames, s.repository],
            (installedPluginNames, repository) => {
                return Object.keys(repository)
                    .filter((name) => !installedPluginNames[name])
                    .map((name) => repository[name])
            },
        ],
        editingPlugin: [
            (s) => [s.editingPluginId, s.installedPlugins],
            (editingPluginId, installedPlugins) =>
                editingPluginId ? installedPlugins.find((plugin) => plugin.id === editingPluginId) : null,
        ],
        loading: [
            (s) => [s.pluginsLoading, s.repositoryLoading, s.pluginConfigsLoading],
            (pluginsLoading, repositoryLoading, pluginConfigsLoading) =>
                pluginsLoading || repositoryLoading || pluginConfigsLoading,
        ],
    },

    events: ({ actions }) => ({
        afterMount: () => {
            actions.loadPlugins()
            actions.loadPluginConfigs()

            if (userLogic.values.user?.plugin_access.install) {
                actions.loadRepository()
            }
        },
    }),
})
