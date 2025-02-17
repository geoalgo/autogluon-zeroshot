import pickle
import sys
from typing import Optional, List

import pandas as pd
from autogluon.common.loaders import load_pkl

from .sim_utils import get_dataset_to_tid_dict, get_dataset_name_to_tid_dict, filter_datasets
from ..utils.rank_utils import RankScorer


class ZeroshotSimulatorContext:
    def __init__(
            self, 
            df_results_by_dataset: pd.DataFrame,
            df_results_by_dataset_automl: pd.DataFrame,
            df_raw: pd.DataFrame, 
            folds: List[int]
    ):
        """
        Encapsulates results evaluated on multiple base models/datasets/folds.
        :param df_results_by_dataset: results of base models on multiple datasets/folds
        :param df_results_by_dataset_automl: results of automl systems by multiple datasets/folds
        :param df_raw: 
        :param folds: List of folds to be considered in a list of integers
        """
        self.folds = folds

        self.df_results_by_dataset_vs_automl, \
        self.df_raw, \
        self.dataset_name_to_tid_dict, \
        self.dataset_to_tid_dict, \
        self.dataset_name_to_fold_dict, \
        self.unique_dataset_folds, \
        self.unique_datasets, \
        self.rank_scorer_vs_automl = self.align_valid_folds(
            df_results_by_dataset=df_results_by_dataset,
            df_results_by_dataset_automl=df_results_by_dataset_automl,
            df_raw=df_raw,
            folds=folds,
        )

        tmp = self.df_results_by_dataset_vs_automl[['dataset', 'tid', 'problem_type']]
        self.dataset_to_problem_type_dict = tmp[['dataset', 'problem_type']].drop_duplicates().set_index(
            'dataset').squeeze().to_dict()
        self.tid_to_problem_type_dict = tmp[['tid', 'problem_type']].drop_duplicates().set_index(
            'tid').squeeze().to_dict()

    @staticmethod
    def align_valid_folds(df_results_by_dataset, df_results_by_dataset_automl, df_raw, folds):
        df_results_by_dataset = df_results_by_dataset[df_results_by_dataset['fold'].isin(folds)]
        unique_dataset_folds_set = set(list(df_results_by_dataset['dataset'].unique()))
        df_results_by_dataset_automl = df_results_by_dataset_automl[
            df_results_by_dataset_automl['dataset'].isin(unique_dataset_folds_set)]

        unique_dataset_folds_set = set(list(df_results_by_dataset_automl['dataset'].unique()))
        df_results_by_dataset, df_raw = filter_datasets(df_results_by_dataset=df_results_by_dataset,
                                                        df_raw=df_raw,
                                                        datasets=unique_dataset_folds_set)

        a = df_results_by_dataset[['tid', 'fold']].drop_duplicates()
        a = a[a['fold'].isin(folds)]
        b = a['tid'].value_counts()
        b = b[b == len(folds)]
        unique_datasets = list(b.index)

        dataset_name_to_fold_dict = df_results_by_dataset[['dataset', 'fold']].drop_duplicates().set_index('dataset')[
            'fold'].to_dict()

        dataset_name_to_tid_dict = get_dataset_name_to_tid_dict(df_raw=df_raw)
        unique_dataset_folds = []
        unique_datasets_set = set(unique_datasets)
        for dataset in unique_dataset_folds_set:
            if dataset_name_to_tid_dict[dataset] in unique_datasets_set:
                unique_dataset_folds.append(dataset)
        unique_dataset_folds_set = set(unique_dataset_folds)

        df_results_by_dataset, df_raw = filter_datasets(df_results_by_dataset=df_results_by_dataset,
                                                        df_raw=df_raw,
                                                        datasets=unique_dataset_folds_set)

        dataset_name_to_tid_dict = get_dataset_name_to_tid_dict(df_raw=df_raw)
        dataset_to_tid_dict = get_dataset_to_tid_dict(df_raw=df_raw)

        automl_error_dict = {}
        for i, dataset in enumerate(unique_dataset_folds):
            automl_error_list = sorted(
                list(df_results_by_dataset_automl[df_results_by_dataset_automl['dataset'] == dataset]['metric_error']))
            automl_error_dict[dataset] = automl_error_list

        rank_scorer_vs_automl = RankScorer(df_results_by_dataset=df_results_by_dataset_automl,
                                           datasets=unique_dataset_folds)
        df_results_by_dataset_vs_automl = df_results_by_dataset.copy()
        df_results_by_dataset_vs_automl['rank'] = [rank_scorer_vs_automl.rank(r[1], r[0]) for r in
                                                   zip(df_results_by_dataset_vs_automl['metric_error'],
                                                       df_results_by_dataset_vs_automl['dataset'])]

        return (
            df_results_by_dataset_vs_automl,
            df_raw,
            dataset_name_to_tid_dict,
            dataset_to_tid_dict,
            dataset_name_to_fold_dict,
            unique_dataset_folds,
            unique_datasets,
            rank_scorer_vs_automl,
        )

    def print_info(self):
        out = '====== Zeroshot Simulator Context Info ======\n'
        out += f'# Configs: {len(self.get_configs())}\n'
        out += f'# Datasets: {len(self.unique_datasets)}\n'
        out += f'# Folds: {len(self.folds)}\n'
        out += f'Folds: {self.folds}\n'
        out += f'# Folds*Datasets: {len(self.unique_dataset_folds)}\n'
        out += '=============================================\n'
        print(out)

    def get_datasets(self, problem_type=None):
        datasets = self.unique_datasets
        if problem_type is not None:
            datasets = [dataset for dataset in datasets if self.tid_to_problem_type_dict[dataset] == problem_type]
        return datasets

    def get_dataset_folds(self, problem_type: Optional[str] = None) -> List[str]:
        """
        :param problem_type: a problem type from AutoGluon in "multiclass", "binary", ...
        :return: List of datasets-folds formatted as `['359987_8', '359933_3', ...]` where the dataset is encoded before
        the "_" and the fold after.
        # Todo/Note it might be clearer to add a column fold in the dataframe and return List[Tuple[str, int]] with
        tuples of dataset/fold.
        """
        datasets = self.unique_dataset_folds
        if problem_type is not None:
            datasets = [dataset for dataset in datasets if self.dataset_to_problem_type_dict[dataset] == problem_type]
        return datasets

    def get_configs(self) -> list:
        """Return all valid configs"""
        return list(self.df_results_by_dataset_vs_automl['framework'].unique())

    def load_zeroshot_pred_proba(self, path_pred_proba, path_gt):
        """
        Loads zeroshot_pred_proba and zeroshot_gt. Minimizes memory usage by popping folds not in self.folds_to_use
        """
        print('Loading zeroshot...')
        zeroshot_gt = load_pkl.load(path_gt)
        # NOTE: This file is BIG (17 GB)
        zeroshot_pred_proba = load_pkl.load(path_pred_proba)
        print('Loading zeroshot successful!')

        zeroshot_gt = {k: v for k, v in zeroshot_gt.items() if k in self.dataset_to_tid_dict}
        zeroshot_gt = {self.dataset_name_to_tid_dict[self.dataset_to_tid_dict[k]]: v for k, v in zeroshot_gt.items()}

        zeroshot_pred_proba = {k: v for k, v in zeroshot_pred_proba.items() if k in self.dataset_to_tid_dict}
        zeroshot_pred_proba = {self.dataset_name_to_tid_dict[self.dataset_to_tid_dict[k]]: v for k, v in
                               zeroshot_pred_proba.items()}

        task_names = list(zeroshot_pred_proba.keys())
        task_names_set = set(task_names)
        for d in self.unique_datasets:
            if d not in task_names_set:
                raise AssertionError(f'Missing expected dataset {d} in zeroshot_pred_proba!')
            folds_in_zs = list(zeroshot_pred_proba[d].keys())
            for f in self.folds:
                if f not in folds_in_zs:
                    raise AssertionError(f'Missing expected fold {f} in dataset {d} in zeroshot_pred_proba! '
                                         f'Expected: {self.folds}, Actual: {folds_in_zs}')

        for d in task_names:
            if d not in self.unique_datasets:
                zeroshot_pred_proba.pop(d)
                zeroshot_gt.pop(d)
            else:
                folds_in_zs = list(zeroshot_pred_proba[d].keys())
                for f in folds_in_zs:
                    if f not in self.folds:
                        zeroshot_pred_proba[d].pop(f)
                        zeroshot_gt[d].pop(f)
        return zeroshot_pred_proba, zeroshot_gt

    @staticmethod
    def minimize_memory_zeroshot_pred_proba(zeroshot_pred_proba: dict, configs: list):
        """
        Minimizes memory usage of zeroshot_pred_proba by popping all model keys not in the input configs list.

        Note: Performs inplace edits.
        """
        size_bytes = sys.getsizeof(pickle.dumps(zeroshot_pred_proba, protocol=4))
        print(f'OLD zeroshot_pred_proba Size: {round(size_bytes / 1e6, 3)} MB')
        task_names = list(zeroshot_pred_proba.keys())
        configs = set(configs)
        for t in task_names:
            available_folds = list(zeroshot_pred_proba[t].keys())
            for f in available_folds:
                model_keys = list(zeroshot_pred_proba[t][f]['pred_proba_dict_val'].keys())
                for k in model_keys:
                    if k not in configs:
                        zeroshot_pred_proba[t][f]['pred_proba_dict_val'].pop(k)
                        zeroshot_pred_proba[t][f]['pred_proba_dict_test'].pop(k)
        size_bytes = sys.getsizeof(pickle.dumps(zeroshot_pred_proba, protocol=4))
        print(f'NEW zeroshot_pred_proba Size: {round(size_bytes / 1e6, 3)} MB')
        return zeroshot_pred_proba
