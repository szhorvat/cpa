from collections import defaultdict
from typing import Union

import torch
import torch.nn as nn
from torch.optim.lr_scheduler import StepLR

from scvi._compat import Literal
from scvi.module.base import BaseModuleClass
from scvi.nn import FCLayers
from scvi.train import TrainingPlan

import numpy as np

class CPATrainingPlan(TrainingPlan):
    def __init__(
        self,
        module: BaseModuleClass,
        covars_to_ncovars: dict,
        autoencoder_lr=1e-3,
        n_steps_kl_warmup: Union[int, None] = None,
        n_epochs_kl_warmup: Union[int, None] = None,
        n_epochs_warmup: Union[int, None] = None,
        adversary_steps: int = 3,
        reg_adversary: int = 5,
        penalty_adversary: int = 3,
        dosers_lr=1e-3,
        dosers_wd=1e-7,
        adversary_lr=3e-4,
        adversary_wd=1e-2,
        autoencoder_wd=1e-6,
        step_size_lr: int = 45,
        batch_size: int = 32,
    ):
        """Training plan for the CPA model"""
        super().__init__(
            module=module,
            lr=autoencoder_lr,
            weight_decay=autoencoder_wd,
            n_steps_kl_warmup=n_steps_kl_warmup,
            n_epochs_kl_warmup=n_epochs_kl_warmup,
            reduce_lr_on_plateau=False,
            lr_factor=None,
            lr_patience=None,
            lr_threshold=None,
            lr_scheduler_metric=None,
            lr_min=None,
        )

        self.n_epochs_warmup = n_epochs_warmup if n_epochs_warmup is not None else 0

        self.covars_to_ncovars = covars_to_ncovars

        # adversarial_models_kwargs = dict(
        #     n_hidden=adversary_width,
        #     n_layers=adversary_depth,
        # )

        self.autoencoder_wd = autoencoder_wd
        self.autoencoder_lr = autoencoder_lr
        
        self.adversary_lr = adversary_lr
        self.adversary_wd = adversary_wd
        self.adversary_steps = adversary_steps
        self.reg_adversary = reg_adversary
        self.penalty_adversary = penalty_adversary
        
        self.dosers_lr = dosers_lr
        self.dosers_wd = dosers_wd

        self.step_size_lr = step_size_lr

        self.batch_size = batch_size

        self.automatic_optimization = False
        self.iter_count = 0

        self.epoch_history = {
            'mode': [], 
            'epoch': [],
            'recon_loss': [], 
            'adv_loss': [], 
            'penalty_adv': [], 
            'adv_drugs': [], 
            'penalty_drugs': [],
            'reg_mean': [],
            'reg_var': [],
            'disent_basal_drugs': [],
            'disent_drugs': []
        }

        for covar in self.covars_to_ncovars.keys():
            self.epoch_history[f'adv_{covar}'] = []
            self.epoch_history[f'penalty_{covar}'] = []

        # Adversarial modules and hparams
        # self.covariates_adv_nn = nn.ModuleDict(
        #     {
        #         key: FCLayers(
        #             n_in=module.n_latent, n_out=n_cats, **adversarial_models_kwargs
        #         )
        #         for key, n_cats in module.cat_to_ncats.items()
        #     }
        # )
        # self.treatments_adv_nn = FCLayers(
        #     n_in=module.n_latent, n_out=module.n_treatments, **adversarial_models_kwargs
        # )
        # self.adv_loss_covariates = nn.CrossEntropyLoss()
        # self.adv_loss_treatments = nn.BCEWithLogitsLoss()
        

    # def _adversarial_classifications(self, z_basal):
    #     """Computes adversarial classifier predictions

    #     Parameters
    #     ----------
    #     z_basal : tensor
    #         Basal states
    #     """
    #     pred_treatments = self.treatments_adv_nn(z_basal)
    #     pred_covariates = dict()
    #     for cat_cov_name in self.module.cat_to_ncats:
    #         pred_covariates[cat_cov_name] = self.covariates_adv_nn[cat_cov_name](
    #             z_basal
    #         )
    #     return pred_treatments, pred_covariates

    # def adversarial_losses(self, tensors, inference_outputs, generative_outputs):
    #     """Computes adversarial classification losses and regularizations"""
    #     z_basal = inference_outputs["z_basal"]
    #     treatments = tensors["treatments"]
    #     c_dict = inference_outputs["c_dict"]
    #     pred_treatments, pred_covariates = self._adversarial_classifications(z_basal)

    #     # Classification losses
    #     adv_cats_loss = 0.0
    #     for cat_cov_name in self.module.cat_to_ncats:
    #         adv_cats_loss += self.adv_loss_covariates(
    #             pred_covariates[cat_cov_name],
    #             c_dict[cat_cov_name].long().squeeze(-1),
    #         )
    #     adv_t_loss = self.adv_loss_treatments(pred_treatments, (treatments > 0).float())
    #     adv_loss = adv_t_loss + adv_cats_loss

    #     # Penalty losses
    #     adv_penalty_cats = 0.0
    #     for cat_cov_name in self.module.cat_to_ncats:
    #         cat_penalty = (
    #             torch.autograd.grad(
    #                 pred_covariates[cat_cov_name].sum(), z_basal, create_graph=True
    #             )[0]
    #             .pow(2)
    #             .mean()
    #         )
    #         adv_penalty_cats += cat_penalty

    #     adv_penalty_treatments = (
    #         torch.autograd.grad(
    #             pred_treatments.sum(),
    #             z_basal,
    #             create_graph=True,
    #         )[0]
    #         .pow(2)
    #         .mean()
    #     )
    #     adv_penalty = adv_penalty_cats + adv_penalty_treatments

    #     return dict(
    #         adv_loss=adv_loss,
    #         adv_penalty=adv_penalty,
    #     )

    def configure_optimizers(self):
        optimizer_autoencoder = torch.optim.Adam(
            list(self.module.encoder.parameters()) +
            list(self.module.decoder.parameters()) +
            list(self.module.drug_network.drug_embedding.parameters()) +
            list(self.module.covars_embedding.parameters()),
            lr=self.autoencoder_lr,
            weight_decay=self.autoencoder_wd)

        optimizer_adversaries = torch.optim.Adam(
            list(self.module.drugs_classifier.parameters()) +
            list(self.module.covars_classifiers.parameters()),
            lr=self.adversary_lr,
            weight_decay=self.adversary_wd)

        optimizer_dosers = torch.optim.Adam(
            self.module.drug_network.dosers.parameters(),
            lr=self.dosers_lr,
            weight_decay=self.dosers_wd)

        # params1 = filter(lambda p: p.requires_grad, self.module.parameters())
        # optimizer1 = torch.optim.Adam(
        #     params1, lr=self.lr, eps=self.autoencoder_wd, weight_decay=self.weight_decay
        # )
        # params2 = filter(
        #     lambda p: p.requires_grad,
        #     list(self.covariates_adv_nn.parameters())
        #     + list(self.treatments_adv_nn.parameters()),
        # )
        # optimizer2 = torch.optim.Adam(
        #     params2,
        #     lr=self.adversary_lr,
        #     eps=self.adversary_wd,
        #     weight_decay=self.weight_decay,
        # )
        # optims = [optimizer1, optimizer2]
        
        optimizers = [optimizer_autoencoder, optimizer_adversaries, optimizer_dosers]
        if self.step_size_lr is not None:
            scheduler_autoencoder = StepLR(optimizer_autoencoder, step_size=self.step_size_lr)
            scheduler_adversaries = StepLR(optimizer_adversaries, step_size=self.step_size_lr)
            scheduler_dosers = StepLR(optimizer_dosers, step_size=self.step_size_lr)
            schedulers = [scheduler_autoencoder, scheduler_adversaries, scheduler_dosers]
            return optimizers, schedulers
        else:
            return optimizers

    def training_step(self, batch, batch_idx):
        opt, opt_adv, opt_dosers = self.optimizers()

        inf_outputs, gen_outputs = self.module.forward(batch, compute_loss=False)
        reconstruction_loss = self.module.loss(
            tensors=batch,
            inference_outputs=inf_outputs,
            generative_outputs=gen_outputs,
        )

        if self.current_epoch >= self.n_epochs_warmup:
            adv_results = self.module.adversarial_loss(tensors=batch,
                                                       inference_outputs=inf_outputs,
                                                       generative_outputs=gen_outputs,
            )

            # Adversarial update
            if self.iter_count % self.adversary_steps != 0:
                opt_adv.zero_grad()
                self.manual_backward(adv_results['adv_loss'] + self.penalty_adversary * adv_results['penalty_adv'])
                opt_adv.step()
            # Model update
            else:
                opt.zero_grad()
                opt_dosers.zero_grad()
                self.manual_backward(reconstruction_loss - self.reg_adversary * adv_results['adv_loss'])
                opt.step()
                opt_dosers.step()
            
            for key, val in adv_results.items():
                adv_results[key] = val.item()
        else:
            adv_results = {'adv_loss': 0.0, 'adv_drugs': 0.0, 'penalty_adv': 0.0, 'penalty_drugs': 0.0}
            for covar in self.covars_to_ncovars.keys():
                adv_results[f'adv_{covar}'] = 0.0
                adv_results[f'penalty_{covar}'] = 0.0

            opt.zero_grad()
            opt_dosers.zero_grad()
            self.manual_backward(reconstruction_loss)
            opt.step()
            opt_adv.step()

        self.iter_count += 1

        # reg_mean, reg_var = self.module.r2_metric(batch, inf_outputs, gen_outputs)
        # disent_drugs, _ = self.module.disentanglement(batch, inf_outputs, gen_outputs)

        results = adv_results.copy()
        results.update({'reg_mean': 0.0, 'reg_var': 0.0})
        results.update({'disent_basal_drugs': 0.0})
        results.update({'disent_drugs': 0.0})
        results.update({'recon_loss': reconstruction_loss.item()})

        return results
        
    def training_epoch_end(self, outputs):
        keys = ['recon_loss', 'adv_loss', 'penalty_adv', 'adv_drugs', 'penalty_drugs', 'reg_mean', 'reg_var', 'disent_basal_drugs', 'disent_drugs']
        for key in keys:
            self.epoch_history[key].append(np.mean([output[key] for output in outputs]))

        for covar in self.covars_to_ncovars.keys():
            key1, key2 = f'adv_{covar}', f'penalty_{covar}'
            self.epoch_history[key1].append(np.mean([output[key1] for output in outputs]))
            self.epoch_history[key2].append(np.mean([output[key2] for output in outputs]))

        self.epoch_history['epoch'].append(self.current_epoch)
        self.epoch_history['mode'].append('train')

        # self.log("recon_loss", self.epoch_history['recon_loss'][-1], prog_bar=True)
        # self.log("adv_loss", self.epoch_history['adv_loss'][-1], prog_bar=True)
        # self.log("penalty_adv", self.epoch_history['penalty_adv'][-1], prog_bar=True)
        # self.log("reg_mean", self.epoch_history['reg_mean'][-1], prog_bar=True)
        # self.log("reg_var", self.epoch_history['reg_var'][-1], prog_bar=True)
        # self.log("disent_drugs", self.epoch_history['disent_drugs'][-1], prog_bar=True)
        
        if self.current_epoch > 1 and self.current_epoch % self.step_size_lr == 0:
            sch, sch_adv, sch_dosers = self.lr_schedulers()
            sch.step()
            sch_adv.step()
            sch_dosers.step()

    def get_progress_bar_dict(self):
        items = super().get_progress_bar_dict()
        items.pop('v_num')
        # items.pop('loss')
        return items
        

    def validation_step(self, batch, batch_idx):
        inf_outputs, gen_outputs = self.module.forward(batch, compute_loss=False)

        reconstruction_loss = self.module.loss(
            tensors=batch,
            inference_outputs=inf_outputs,
            generative_outputs=gen_outputs,
        )

        # if self.current_epoch >= self.n_epochs_warmup:
        #     adv_results = self.module.adversarial_loss(
        #         tensors=batch,
        #         inference_outputs=inf_outputs,
        #         generative_outputs=gen_outputs,
        #     )
        #     for key, val in adv_results.items():
        #         adv_results[key] = val.item()
        # else:
        adv_results = {'adv_loss': 0.0, 'adv_drugs': 0.0, 'penalty_adv': 0.0, 'penalty_drugs': 0.0}
        for covar in self.covars_to_ncovars.keys():
            adv_results[f'adv_{covar}'] = 0.0
            adv_results[f'penalty_{covar}'] = 0.0

        r2_mean, r2_var = self.module.r2_metric(batch, inf_outputs, gen_outputs)
        disent_basal_drugs, disent_drugs = self.module.disentanglement(batch, inf_outputs, gen_outputs)

        results = adv_results
        results.update({'reg_mean': r2_mean, 'reg_var': r2_var})
        results.update({'disent_basal_drugs': disent_basal_drugs})
        results.update({'disent_drugs': disent_drugs})
        results.update({'recon_loss': reconstruction_loss.item()})
        results.update({'cpa_metric': r2_mean + 1.0 - disent_basal_drugs + disent_drugs})

        return results

    def validation_epoch_end(self, outputs):
        keys = ['recon_loss', 'adv_loss', 'penalty_adv', 'adv_drugs', 'penalty_drugs', 'reg_mean', 'reg_var', 'disent_basal_drugs', 'disent_drugs']
        for key in keys:
            self.epoch_history[key].append(np.mean([output[key] for output in outputs]))

        for covar in self.covars_to_ncovars.keys():
            key1, key2 = f'adv_{covar}', f'penalty_{covar}'
            self.epoch_history[key1].append(np.mean([output[key1] for output in outputs]))
            self.epoch_history[key2].append(np.mean([output[key2] for output in outputs]))

        self.epoch_history['epoch'].append(self.current_epoch)
        self.epoch_history['mode'].append('valid')

        # self.log('val_recon_loss', self.epoch_history['recon_loss'][-1], prog_bar=True)
        self.log('cpa_metric', np.mean([output['cpa_metric'] for output in outputs]), prog_bar=True)
        self.log('val_reg_mean', self.epoch_history['reg_mean'][-1], prog_bar=True)
        self.log('val_disent_basal_drugs', self.epoch_history['disent_basal_drugs'][-1], prog_bar=True)
        self.log('val_disent_drugs', self.epoch_history['disent_drugs'][-1], prog_bar=True)
        self.log('val_reg_var', self.epoch_history['reg_var'][-1], prog_bar=True)
        
    # def on_validation_epoch_start(self) -> None:
    #     torch.set_grad_enabled(True)

    # def on_validation_epoch_end(self) -> None:
    #     self.zero_grad()