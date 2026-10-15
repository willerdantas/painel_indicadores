-- Schema "painel": dados lançados pelo painel (custos, produção hospitalar e SIMAQ).
--
-- Só é necessário se o usuário configurado no painel não puder criar schemas
-- (se puder, o painel cria sozinho). Executar UMA vez, como superusuário (ou
-- dono do banco), no mesmo banco configurado no painel. Como o usuário do
-- painel fica dono do schema, ele mesmo cria as tabelas. O nome do schema é o
-- informado em "Schema dos dados do painel" na configuração do banco.
--
-- Depois disso, ao subir (ou em até 1 minuto, se já estiver rodando), o painel
-- cria as tabelas abaixo e migra as planilhas de dados/ para elas
-- (banco_painel.py). Se o usuário do painel for outro, troque o nome em
-- AUTHORIZATION.

CREATE SCHEMA IF NOT EXISTS painel AUTHORIZATION painel;

-- Tabelas criadas pelo painel (referência):
--
-- painel.custos              (mes, tipo, item, valor)
-- painel.producao_hospitalar (mes, servico, tipo, procedimento, quantidade)
-- painel.simaq               (mes, indicador, tipo, numerador, denominador, resultado, meta)
--
-- Todas com id, atualizado_em e atualizado_por, e UNIQUE (mes, chaves do item).
-- "mes" é DATE no dia 1 do mês de referência.
