targetScope = 'resourceGroup'

@description('Existing Linux API Web App on a plan supporting regional VNet integration (B1 or higher).')
param apiName string

@description('Region of the existing API and VNet.')
param location string

@description('Existing platform VNet containing the ledger private endpoint and linked private DNS zone.')
param virtualNetworkName string

param apiSubnetName string = 'snet-api'

@description('Unused subnet range inside the existing VNet; must not overlap another subnet.')
param apiSubnetAddressPrefix string = '10.42.3.64/27'

@description('Optional existing Free plan to restore to B1 after reviewing cost and all hosted apps. Empty preserves the current plan.')
param restoreFreePlanName string = ''

param apimName string = ''

@secure()
param currentApimConfiguration object = {}

param existingNetworkSecurityGroupId string = ''

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource apiSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: virtualNetwork
  name: apiSubnetName
  properties: {
    addressPrefix: apiSubnetAddressPrefix
    networkSecurityGroup: empty(existingNetworkSecurityGroupId) ? null : {
      id: existingNetworkSecurityGroupId
    }
    delegations: [
      {
        name: 'app-service-delegation'
        properties: {
          serviceName: 'Microsoft.Web/serverFarms'
        }
      }
    ]
  }
}

resource api 'Microsoft.Web/sites@2024-11-01' existing = {
  name: apiName
}

resource restoredApiPlan 'Microsoft.Web/serverfarms@2024-11-01' = if (!empty(restoreFreePlanName)) {
  name: restoreFreePlanName
  location: location
  kind: 'linux'
  sku: {
    name: 'B1'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource apiVnetIntegration 'Microsoft.Web/sites/networkConfig@2024-11-01' = if (empty(apimName)) {
  parent: api
  name: 'virtualNetwork'
  properties: {
    subnetResourceId: apiSubnet.id
    swiftSupported: true
  }
  dependsOn: [
    restoredApiPlan
  ]
}

resource apimOutboundIntegration 'Microsoft.ApiManagement/service@2024-05-01' = if (!empty(apimName)) {
  name: apimName
  location: location
  sku: currentApimConfiguration.sku
  identity: currentApimConfiguration.identity
  tags: currentApimConfiguration.tags
  properties: union(currentApimConfiguration.properties, {
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: {
      subnetResourceId: apiSubnet.id
    }
  })
}
