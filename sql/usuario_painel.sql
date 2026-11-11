-- Usuário do PostgreSQL só com o acesso de que o painel precisa (em vez do
-- postgres, que é superusuário). Rode uma vez como superusuário, conectado ao
-- banco do sistema hospitalar; depois informe painel_app e a senha em
-- Administração > Configuração do Banco e teste a conexão.
--
-- Ajuste antes de rodar: a senha, o nome do banco (integra_local), o schema do
-- sistema hospitalar (public) e o schema dos dados do painel (painel).

CREATE ROLE painel_app LOGIN PASSWORD 'troque-esta-senha';

-- sistema hospitalar: só leitura (o painel nunca grava nele)
GRANT CONNECT ON DATABASE integra_local TO painel_app;
GRANT USAGE ON SCHEMA public TO painel_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO painel_app;
-- tabelas criadas depois (pelo postgres) também ficam legíveis
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO painel_app;

-- dados do painel (lançamentos, cadastros, custos): o painel cria e altera as
-- próprias tabelas, então fica dono do schema e do que já existe nele
CREATE SCHEMA IF NOT EXISTS painel;
ALTER SCHEMA painel OWNER TO painel_app;
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'painel' AND c.relkind IN ('r', 'p', 'v', 'm') LOOP
        -- as sequências dos campos serial acompanham a tabela
        EXECUTE format('ALTER %s painel.%I OWNER TO painel_app',
                       CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,
                       r.relname);
    END LOOP;
END $$;
