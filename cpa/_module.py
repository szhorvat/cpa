import numpy as np
import torch
import torch.nn as nn
from scvi import settings
from scvi.distributions import NegativeBinomial, ZeroInflatedNegativeBinomial
from scvi.module import Classifier
from scvi.module.base import BaseModuleClass, auto_move_data
from scvi.nn import Encoder, DecoderSCVI
from torch.distributions import Normal
from torch.distributions.kl import kl_divergence as kl
from torchmetrics.functional import accuracy, pearson_corrcoef, r2_score

from ._metrics import knn_purity
from ._utils import PerturbationNetwork, VanillaEncoder, CPA_REGISTRY_KEYS

from typing import Optional


class CPAModule(BaseModuleClass):
    """
    CPA module using Gaussian/NegativeBinomial Likelihood

    Parameters
    ----------
        n_genes: int
            Number of input genes
        n_treatments: int
            Number of total different treatments (single/combinatorial)
        covars_encoder: dict
            Dictionary of covariates with keys as each covariate name and values as
                number of unique values of the corresponding covariate
        n_latent: int
            dimensionality of the latent space
        recon_loss: str
            Autoencoder loss (either "gauss", "nb" or "zinb")
        doser_type: str
            Type of dosage network (either "logsigm", "sigm", or "linear")
        n_hidden_encoder: int

        n_layers_encoder: int

        use_batch_norm_encoder: bool

        use_layer_norm_encoder: bool

        variational: bool
    """

    def __init__(self,
                 n_genes: int,
                 n_perts: int,
                 covars_encoder: dict,
                 n_latent: int = 128,
                 recon_loss: str = "nb",
                 doser_type: str = "logsigm",
                 n_hidden_encoder: int = 256,
                 n_layers_encoder: int = 3,
                 n_hidden_decoder: int = 256,
                 n_layers_decoder: int = 3,
                 n_hidden_doser: int = 128,
                 n_layers_doser: int = 2,
                 use_batch_norm_encoder: bool = True,
                 use_layer_norm_encoder: bool = False,
                 use_batch_norm_decoder: bool = True,
                 use_layer_norm_decoder: bool = False,
                 dropout_rate_encoder: float = 0.0,
                 dropout_rate_decoder: float = 0.0,
                 variational: bool = False,
                 seed: int = 0,
                 ):
        super().__init__()

        recon_loss = recon_loss.lower()
        assert recon_loss in ['gauss', 'zinb', 'nb']

        torch.manual_seed(seed)
        np.random.seed(seed)
        settings.seed = seed

        self.n_genes = n_genes
        self.n_perts = n_perts
        self.n_latent = n_latent
        self.recon_loss = recon_loss
        self.doser_type = doser_type
        self.variational = variational

        self.covars_encoder = covars_encoder

        if variational:
            self.encoder = Encoder(
                n_genes,
                n_latent,
                var_activation=nn.Softplus(),
                n_hidden=n_hidden_encoder,
                n_layers=n_layers_encoder,
                use_batch_norm=use_batch_norm_encoder,
                use_layer_norm=use_layer_norm_encoder,
                dropout_rate=dropout_rate_encoder,
                activation_fn=nn.ReLU,
                return_dist=True,
            )
        else:
            self.encoder = VanillaEncoder(
                n_input=n_genes,
                n_output=n_latent,
                n_cat_list=[],
                n_hidden=n_hidden_encoder,
                n_layers=n_layers_encoder,
                use_batch_norm=use_batch_norm_encoder,
                use_layer_norm=use_layer_norm_encoder,
                dropout_rate=dropout_rate_encoder,
                activation_fn=nn.ReLU,
                output_activation='linear',
            )

        # Decoder components
        if self.recon_loss in ['zinb', 'nb']:
            # setup the parameters of your generative model, as well as your inference model
            self.px_r = torch.nn.Parameter(torch.randn(self.n_genes))

            # l encoder goes from n_input-dimensional data to 1-d library size
            self.library_encoder = Encoder(
                self.n_genes,
                1,
                n_layers=1,
                n_hidden=128,
                dropout_rate=dropout_rate_decoder,
                return_dist=True,
            )

            # decoder goes from n_latent-dimensional space to n_input-d data
            self.decoder = DecoderSCVI(
                n_input=n_latent,
                n_output=n_genes,
                n_layers=n_layers_decoder,
                n_hidden=n_hidden_decoder,
                use_batch_norm=use_batch_norm_decoder,
                use_layer_norm=use_layer_norm_decoder,
            )

        elif recon_loss == "gauss":
            self.decoder = Encoder(n_input=n_latent,
                                   n_output=n_genes,
                                   n_layers=n_layers_decoder,
                                   n_hidden=n_hidden_decoder,
                                   dropout_rate=dropout_rate_decoder,
                                   use_batch_norm=use_batch_norm_decoder,
                                   use_layer_norm=use_layer_norm_decoder,
                                   var_activation=None,
                                   )

        else:
            raise Exception('Invalid Loss function for Autoencoder')

        # Embeddings
        # 1. Drug Network
        self.pert_network = PerturbationNetwork(n_perts=n_perts,
                                                n_latent=n_latent,
                                                doser_type=doser_type,
                                                n_hidden=n_hidden_doser,
                                                n_layers=n_layers_doser,
                                                )

        # 2. Covariates Embedding
        self.covars_embeddings = nn.ModuleDict(
            {
                key: torch.nn.Embedding(len(unique_covars), n_latent)
                for key, unique_covars in self.covars_encoder.items()
            }
        )

        self.metrics = {
            'pearson_r': pearson_corrcoef,
            'r2_score': r2_score
        }

    def mixup_data(self, tensors, alpha: float = 0.0, opt=False):
        """
            Returns mixed inputs, pairs of targets, and lambda
        """
        alpha = max(0.0, alpha)

        if alpha == 0.0:
            mixup_lambda = 1.0
        else:
            mixup_lambda = np.random.beta(alpha, alpha)

        x = tensors[CPA_REGISTRY_KEYS.X_KEY]
        y_perturbations = tensors[CPA_REGISTRY_KEYS.PERTURBATION_KEY]
        perturbations = tensors[CPA_REGISTRY_KEYS.PERTURBATIONS]
        perturbations_dosages = tensors[CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES]

        batch_size = x.size()[0]
        index = torch.randperm(batch_size).to(x.device)

        mixed_x = mixup_lambda * x + (1. - mixup_lambda) * x[index, :]

        tensors[CPA_REGISTRY_KEYS.X_KEY] = mixed_x
        tensors[CPA_REGISTRY_KEYS.X_KEY + '_true'] = x
        tensors[CPA_REGISTRY_KEYS.X_KEY + '_mixup'] = x[index]
        tensors[CPA_REGISTRY_KEYS.PERTURBATION_KEY + '_mixup'] = y_perturbations[index]
        tensors[CPA_REGISTRY_KEYS.PERTURBATIONS + '_mixup'] = perturbations[index]
        tensors[CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES + '_mixup'] = perturbations_dosages[index]

        for covar, encoder in self.covars_encoder.items():
            tensors[covar + '_mixup'] = tensors[covar][index]

        return tensors, mixup_lambda

    def _get_inference_input(self, tensors):
        x = tensors[CPA_REGISTRY_KEYS.X_KEY]  # batch_size, n_genes
        perts = {
            'true': tensors[CPA_REGISTRY_KEYS.PERTURBATIONS],
            'mixup': tensors[CPA_REGISTRY_KEYS.PERTURBATIONS + '_mixup']
        }
        perts_doses = {
            'true': tensors[CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES],
            'mixup': tensors[CPA_REGISTRY_KEYS.PERTURBATIONS_DOSAGES + '_mixup'],
        }

        covars_dict = dict()
        for covar, unique_covars in self.covars_encoder.items():
            encoded_covars = tensors[covar].view(-1, )  # (batch_size,)
            encoded_covars_mixup = tensors[covar + '_mixup'].view(-1, )  # (batch_size,)
            covars_dict[covar] = encoded_covars
            covars_dict[covar + '_mixup'] = encoded_covars_mixup

        return dict(
            x=x,
            perts=perts,
            perts_doses=perts_doses,
            covars_dict=covars_dict,
        )

    @auto_move_data
    def inference(
            self,
            x,
            perts,
            perts_doses,
            covars_dict,
            mixup_lambda: float = 1.0,
            n_samples: int = 1,
    ):
        batch_size = x.shape[0]

        if self.recon_loss in ['nb', 'zinb']:
            # log the input to the variational distribution for numerical stability
            x_ = torch.log(1 + x)
            ql, library = self.library_encoder(x_)
        else:
            x_ = x
            ql, library = None, None

        if self.variational:
            qz, z_basal = self.encoder(x_)
        else:
            qz, z_basal = None, self.encoder(x_)

        if self.variational and n_samples > 1:
            sampled_z = qz.sample((n_samples,))
            z_basal = self.encoder.z_transformation(sampled_z)
            if self.recon_loss in ['nb', 'zinb']:
                library = ql.sample((n_samples,))

        z_pert_true = self.pert_network(perts['true'], perts_doses['true'])
        if mixup_lambda < 1.0:
            z_pert_mixup = self.pert_network(perts['mixup'], perts_doses['mixup'])
            z_pert = mixup_lambda * z_pert_true + (1. - mixup_lambda) * z_pert_mixup
        else:
            z_pert = z_pert_true

        z_covs = torch.zeros_like(z_basal)  # ([n_samples,] batch_size, n_latent)
        for covar, encoder in self.covars_encoder.items():
            z_cov = self.covars_embeddings[covar](covars_dict[covar].long())
            if len(encoder) > 1:
                z_cov_mixup = self.covars_embeddings[covar](covars_dict[covar + '_mixup'].long())
                z_cov = mixup_lambda * z_cov + (1. - mixup_lambda) * z_cov_mixup
            z_cov = z_cov.view(batch_size, self.n_latent)  # batch_size, n_latent
            z_covs += z_cov

        z = z_basal + z_pert + z_covs

        return dict(
            z=z,
            z_basal=z_basal,
            z_covs=z_covs,
            z_pert=z_pert.sum(dim=1),
            library=library,
            qz=qz,
            ql=ql,
            mixup_lambda=mixup_lambda,
        )

    def _get_generative_input(self, tensors, inference_outputs, **kwargs):
        z = inference_outputs["z"]
        library = inference_outputs['library']

        return dict(
            z=z,
            library=library,
        )

    @auto_move_data
    def generative(
            self,
            z,
            library=None,
    ):
        if self.recon_loss == 'nb':
            px_scale, _, px_rate, px_dropout = self.decoder("gene", z, library)
            px_r = torch.exp(self.px_r)

            px = NegativeBinomial(mu=px_rate, theta=px_r)

        elif self.recon_loss == 'zinb':
            px_scale, _, px_rate, px_dropout = self.decoder("gene", z, library)
            px_r = torch.exp(self.px_r)

            px = ZeroInflatedNegativeBinomial(mu=px_rate, theta=px_r, zi_logits=px_dropout)

        else:
            px_mean, px_var, x_pred = self.decoder(z)

            px = Normal(loc=px_mean, scale=px_var.sqrt())

        pl = None
        pz = Normal(torch.zeros_like(z), torch.ones_like(z))
        return dict(px=px, pz=pz, pl=pl)

    def loss(self, tensors, inference_outputs, generative_outputs):
        """Computes the reconstruction loss (AE) or the ELBO (VAE)"""
        x = tensors[CPA_REGISTRY_KEYS.X_KEY]

        px = generative_outputs['px']
        recon_loss = -px.log_prob(x).sum(dim=-1).mean()

        if self.variational:
            qz = inference_outputs["qz"]
            pz = generative_outputs['pz']

            kl_divergence_z = kl(qz, pz).sum(dim=1)
            kl_loss = kl_divergence_z.mean()
        else:
            kl_loss = torch.zeros_like(recon_loss)

        return recon_loss, kl_loss

    def r2_metric(self, tensors, inference_outputs, generative_outputs, mode: str = 'lfc'):
        mode = mode.lower()
        assert mode in ['direct']

        x = tensors[CPA_REGISTRY_KEYS.X_KEY]  # batch_size, n_genes

        r2_mean = torch.tensor(0.0, device=x.device)
        r2_var = torch.tensor(0.0, device=x.device)

        px = generative_outputs['px']
        if self.recon_loss == 'gauss':
            x_pred_mean = px.loc
            x_pred_var = px.scale ** 2

            if CPA_REGISTRY_KEYS.DEG_MASK_R2 in tensors.keys():
                deg_mask = tensors[f'{CPA_REGISTRY_KEYS.DEG_MASK_R2}']

                x *= deg_mask
                x_pred_mean *= deg_mask
                x_pred_var *= deg_mask

            x_pred_mean = torch.nan_to_num(x_pred_mean, nan=0, posinf=1e3, neginf=-1e3)
            x_pred_var = torch.nan_to_num(x_pred_var, nan=0, posinf=1e3, neginf=-1e3)

            r2_mean = torch.nan_to_num(self.metrics['r2_score'](x_pred_mean.mean(0), x.mean(0)),
                                       nan=0.0).item()
            r2_var = torch.nan_to_num(self.metrics['r2_score'](x_pred_var.mean(0), x.var(0)),
                                      nan=0.0).item()

        elif self.recon_loss in ['nb', 'zinb']:
            x = torch.log(1 + x)
            x_pred = px.mu
            x_pred = torch.log(1 + x_pred)

            x_pred = torch.nan_to_num(x_pred, nan=0, posinf=1e3, neginf=-1e3)

            if CPA_REGISTRY_KEYS.DEG_MASK_R2 in tensors.keys():
                deg_mask = tensors[f'{CPA_REGISTRY_KEYS.DEG_MASK_R2}']

                x *= deg_mask
                x_pred *= deg_mask

            r2_mean = torch.nan_to_num(self.metrics['r2_score'](x_pred.mean(0), x.mean(0)),
                                       nan=0.0).item()
            r2_var = torch.nan_to_num(self.metrics['r2_score'](x_pred.var(0), x.var(0)),
                                      nan=0.0).item()

        return r2_mean, r2_var

    def disentanglement(self, tensors, inference_outputs, generative_outputs, linear=True):
        z_basal = inference_outputs['z_basal'].detach().cpu().numpy()
        z = inference_outputs['z'].detach().cpu().numpy()

        perturbations = tensors[CPA_REGISTRY_KEYS.PERTURBATION_KEY].view(-1, )
        perturbations_names = perturbations.detach().cpu().numpy()

        knn_basal = knn_purity(z_basal, perturbations_names.ravel(),
                               n_neighbors=min(perturbations_names.shape[0] - 1, 30))
        knn_after = knn_purity(z, perturbations_names.ravel(),
                               n_neighbors=min(perturbations_names.shape[0] - 1, 30))

        for covar, unique_covars in self.covars_encoder.items():
            if len(unique_covars) > 1:
                target_covars = tensors[f'{covar}'].detach().cpu().numpy()

                knn_basal += knn_purity(z_basal, target_covars.ravel(),
                                        n_neighbors=min(target_covars.shape[0] - 1, 30))

                knn_after += knn_purity(z, target_covars.ravel(),
                                        n_neighbors=min(target_covars.shape[0] - 1, 30))

        return knn_basal, knn_after

    def get_expression(self, tensors, n_samples=1):
        """Computes gene expression means and std.

        Only implemented for the gaussian likelihood.

        Parameters
        ----------
        tensors : dict
            Considered inputs

        """
        tensors, _ = self.mixup_data(tensors, alpha=0.0)

        _, generative_outputs = self.forward(
            tensors,
            inference_kwargs={'n_samples': n_samples},
            compute_loss=False,
        )

        px = generative_outputs['px']

        if self.recon_loss == 'gauss':
            output_key = 'loc'
        else:
            output_key = 'mu'

        output = getattr(px, output_key)

        return output

    def get_pert_embeddings(self, tensors, **inference_kwargs):
        inputs = self._get_inference_input(tensors)
        drugs = inputs['perts']
        doses = inputs['perts_doses']

        return self.pert_network(drugs, doses)
