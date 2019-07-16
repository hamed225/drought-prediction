import numpy as np
import random
from pathlib import Path
import math

import torch
from torch.nn import functional as F

import shap

from typing import Any, Dict, List, Optional, Tuple, Union

from ..base import ModelBase
from ..utils import chunk_array
from ..data import DataLoader, train_val_mask


class NNBase(ModelBase):

    def __init__(self,
                 data_folder: Path = Path('data'),
                 batch_size: int = 1,
                 pred_months: Optional[List[int]] = None,
                 include_pred_month: bool = True,
                 surrounding_pixels: Optional[int] = None) -> None:
        super().__init__(data_folder, batch_size, pred_months, include_pred_month,
                         surrounding_pixels)

        # for reproducibility
        torch.manual_seed(42)

        self.explainer: Optional[shap.DeepExplainer] = None

    def explain(self, x: Any) -> np.ndarray:
        assert self.model is not None, 'Model must be trained!'

        if self.explainer is None:
            background_samples = self._get_background(sample_size=100)
            self.explainer: shap.DeepExplainer = shap.DeepExplainer(
                self.model, background_samples)
        if self.include_pred_month:
            assert type(x) == list, \
                'include_pred_month is True, so this model expects a list of tensors as input'
            if len(x[1].shape) == 1:
                x[1] = self._one_hot_months(x[1])

        return self.explainer.shap_values(x)

    def _initialize_model(self, x_ref: Tuple[torch.Tensor, ...]) -> torch.nn.Module:
        raise NotImplementedError

    def train(self, num_epochs: int = 1,
              early_stopping: Optional[int] = None,
              batch_size: int = 256,
              learning_rate: float = 1e-3,
              val_split: float = 0.1) -> None:
        print(f'Training {self.model_name}')

        if early_stopping is not None:
            len_mask = len(DataLoader._load_datasets(self.data_path, mode='train',
                                                     shuffle_data=False,
                                                     pred_months=self.pred_months))
            train_mask, val_mask = train_val_mask(len_mask, val_split)

            train_dataloader = DataLoader(data_path=self.data_path,
                                          batch_file_size=self.batch_size,
                                          shuffle_data=True, mode='train', mask=train_mask,
                                          pred_months=self.pred_months, to_tensor=True,
                                          surrounding_pixels=self.surrounding_pixels)
            val_dataloader = DataLoader(data_path=self.data_path,
                                        batch_file_size=self.batch_size,
                                        shuffle_data=False, mode='train', mask=val_mask,
                                        pred_months=self.pred_months, to_tensor=True,
                                        surrounding_pixels=self.surrounding_pixels)
            batches_without_improvement = 0
            best_val_score = np.inf
        else:
            train_dataloader = DataLoader(data_path=self.data_path,
                                          batch_file_size=self.batch_size,
                                          shuffle_data=True, mode='train',
                                          pred_months=self.pred_months, to_tensor=True,
                                          surrounding_pixels=self.surrounding_pixels)

        # initialize the model
        if self.model is None:
            x_ref, _ = next(iter(train_dataloader))
            model = self._initialize_model(x_ref)
            self.model = model

        optimizer = torch.optim.Adam([pam for pam in self.model.parameters()],
                                     lr=learning_rate)

        for epoch in range(num_epochs):
            train_rmse = []
            self.model.train()
            for x, y in train_dataloader:
                for x_batch, y_batch in chunk_array(x, y, batch_size, shuffle=True):
                    optimizer.zero_grad()
                    pred = self.model(x_batch[0], self._one_hot_months(x_batch[1]))
                    loss = F.smooth_l1_loss(pred, y_batch)
                    loss.backward()
                    optimizer.step()

                    train_rmse.append(loss.item())

            if early_stopping is not None:
                self.model.eval()
                val_rmse = []
                with torch.no_grad():
                    for x, y in val_dataloader:
                        val_pred_y = self.model(x[0], self._one_hot_months(x[1]))
                        val_loss = F.mse_loss(val_pred_y, y)

                        val_rmse.append(math.sqrt(val_loss.item()))

            print(f'Epoch {epoch + 1}, train RMSE: {np.mean(train_rmse)}')

            if early_stopping is not None:
                epoch_val_rmse = np.mean(val_rmse)
                print(f'Val RMSE: {epoch_val_rmse}')
                if epoch_val_rmse < best_val_score:
                    batches_without_improvement = 0
                    best_val_score = epoch_val_rmse
                    best_model_dict = self.model.state_dict()
                else:
                    batches_without_improvement += 1
                    if batches_without_improvement == early_stopping:
                        print('Early stopping!')
                        self.model.load_state_dict(best_model_dict)
                        return None

    def predict(self) -> Tuple[Dict[str, Dict[str, np.ndarray]], Dict[str, np.ndarray]]:
        test_arrays_loader = DataLoader(data_path=self.data_path, batch_file_size=self.batch_size,
                                        shuffle_data=False, mode='test',
                                        pred_months=self.pred_months, to_tensor=True,
                                        surrounding_pixels=self.surrounding_pixels)

        preds_dict: Dict[str, np.ndarray] = {}
        test_arrays_dict: Dict[str, Dict[str, np.ndarray]] = {}

        assert self.model is not None, 'Model must be trained before predictions can be generated'

        self.model.eval()
        with torch.no_grad():
            for dict in test_arrays_loader:
                for key, val in dict.items():
                    preds = self.model(val.x.historical, self._one_hot_months(val.x.pred_months))
                    preds_dict[key] = preds.numpy()
                    test_arrays_dict[key] = {'y': val.y.numpy(), 'latlons': val.latlons}

        return test_arrays_dict, preds_dict

    def _get_background(self, sample_size: int = 100) -> Union[torch.Tensor,
                                                               List[torch.Tensor]]:

        print('Extracting a sample of the training data')

        train_dataloader = DataLoader(data_path=self.data_path,
                                      batch_file_size=self.batch_size,
                                      shuffle_data=True, mode='train',
                                      pred_months=self.pred_months,
                                      to_tensor=True,
                                      surrounding_pixels=self.surrounding_pixels)
        output_tensors: List[torch.Tensor] = []
        if self.include_pred_month:
            output_pred_months: List[torch.Tensor] = []
        samples_per_instance = max(1, sample_size // len(train_dataloader))

        for x, _ in train_dataloader:
            while len(output_tensors) < sample_size:
                for _ in range(samples_per_instance):
                    idx = random.randint(0, x[0].shape[0] - 1)
                    output_tensors.append(x[0][idx])
                    if self.include_pred_month:
                        output_pred_months.append(self._one_hot_months(x[1][idx: idx + 1]))
        if self.include_pred_month:
            return [torch.stack(output_tensors), torch.cat(output_pred_months, dim=0)]
        else:
            return torch.stack(output_tensors)

    @staticmethod
    def _one_hot_months(indices: torch.Tensor) -> torch.Tensor:
        return torch.eye(14)[indices.long()][:, 1:-1]