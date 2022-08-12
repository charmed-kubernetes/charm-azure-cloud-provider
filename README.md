# azure-cloud-provider

## Description

This subordinate charm manages the Cloud Controller and AzureDisk Storage controller components of Azure.

## Usage

The charm requires azure credentials and connection information, which
can be provided either directly, via config, or via the `azure-integration`
relation to the [Azure Integrator charm](https://charmhub.io/azure-integrator).

## Deployment

### The full process

```bash
juju deploy charmed-kubernetes
juju deploy azure-integrator --trust
juju deploy azure-cloud-provider

juju relate azure-cloud-provider:certificates            easyrsa
juju relate azure-cloud-provider:kube-control            kubernetes-control-plane
juju relate azure-cloud-provider:external-cloud-provider kubernetes-control-plane
juju relate azure-cloud-provider                         azure-integrator

##  wait for the azure node controller daemonset to be running
##  wait for the azure control manager deployment to be running
kubectl describe nodes |egrep "Taints:|Name:|Provider"
```

### Details

* Requires a `charmed-kubernetes` deployment on a azure cloud launched by juju
* Deploy the `azure-integrator` charm into the model using `--trust` so juju provided azure credentials
* Deploy the `azure-cloud-provider` charm in the model relating to the integrator and to charmed-kubernetes components
* Once the model is active/idle, the cloud-provider charm will have successfully deployed the azure cloud controller and an azure-disk storage controller on each node in the kube-system namespace.
* Confirm the `ProviderID` is set on each node
* Confirm the Storage Class `azure-csi-default` is created


## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/charmed-kubernetes/charm-azure-cloud-provider/blob/main/CONTRIBUTING.md)
for developer guidance.
